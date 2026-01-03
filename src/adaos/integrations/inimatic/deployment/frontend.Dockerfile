FROM node:20.18.0 AS build
WORKDIR /inimatic

# имя npm-скрипта, которое будем выполнять
ARG BUILD_SCRIPT=buildprod
ARG BUILD_VERSION=prod
ENV BUILD_VERSION=$BUILD_VERSION

COPY package*.json ./

# подавляем postinstall, ускоряем CI
ENV npm_config_ignore_scripts=true \
    npm_config_legacy_peer_deps=true \
    npm_config_audit=false \
    npm_config_fund=false \
    NX_BINARY_SKIP_DOWNLOAD=true \
    NX_NATIVE=false \
    CI=1

RUN (npm ci) || (echo "npm ci failed, fallback to npm install" && npm install)

# точечно достраиваем esbuild
RUN npm_config_ignore_scripts=false npm rebuild esbuild

COPY ./ /inimatic

# если ARG пустой или скрипта нет — упадём с понятной ошибкой
RUN test -n "$BUILD_SCRIPT" || (echo "BUILD_SCRIPT is empty" && exit 2) \
 && npm run "$BUILD_SCRIPT" --if-present \
 || (echo "npm script '$BUILD_SCRIPT' not found. Add it to package.json or pass a correct BUILD_SCRIPT." && exit 3)

# sanity-check: убедимся, что артефакт реально собран
RUN test -d /inimatic/www -o -d /inimatic/dist || (echo "No build output (www/ or dist/) found" && exit 4)

# --- runtime stage ---
FROM nginx:latest
COPY --from=build /inimatic/www /usr/share/nginx/html
# если у тебя Angular выводит в dist/<app>, можно заменить на dist
# COPY --from=build /inimatic/dist /usr/share/nginx/html

COPY ./deployment/nginx/default.conf /etc/nginx/conf.d/default.conf
EXPOSE 8080
