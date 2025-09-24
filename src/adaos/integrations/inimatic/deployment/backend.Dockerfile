# backend.Dockerfile
FROM node:20.18.0
WORKDIR /inimatic_backend

# 1) тянем только манифесты
COPY ./package*.json ./

# 2) отключаем скрипты и лечим peer-deps; блокируем nx native
ENV npm_config_ignore_scripts=true \
    npm_config_legacy_peer_deps=true \
    npm_config_audit=false \
    npm_config_fund=false \
    NX_BINARY_SKIP_DOWNLOAD=true \
    NX_NATIVE=false \
    CI=1

# 3) установка зависимостей без запуска postinstall/preinstall
#    если нет lock-файла — fallback на install
RUN (npm ci) || (echo "npm ci failed, falling back to npm install" && npm install)

# 4) код backend
COPY ./backend ./backend

# 5) компилируем backend напрямую, минуя скрипты npm (чтобы точно не дергать nx)
#    используем фиксированную версию tsc, чтобы не зависеть от devDeps корня
RUN npx --yes --package typescript@5.6.3 tsc -p backend/tsconfig.backend.json

EXPOSE 3030

# 6) запускаем собранный JS (без ts-node)
CMD ["node", "dist/out-tsc/app.js"]
