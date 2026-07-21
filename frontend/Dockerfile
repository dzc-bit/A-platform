FROM node:22-alpine AS build
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN corepack enable && pnpm install --frozen-lockfile
COPY . .
RUN pnpm run build

FROM nginx:1.27-alpine
COPY --chown=101:101 --from=build /app/dist /usr/share/nginx/html
COPY --chown=101:101 nginx.conf /etc/nginx/conf.d/default.conf
RUN mkdir -p /run \
    && chown 101:101 /run \
    && chown -R 101:101 /var/cache/nginx /usr/share/nginx/html /etc/nginx/conf.d

USER 101:101

EXPOSE 8080
