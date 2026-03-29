FROM node:20.18.0 AS build
WORKDIR /inimatic

ARG BUILD_SCRIPT=buildprod
ARG BUILD_VERSION=prod
ENV BUILD_VERSION=$BUILD_VERSION

COPY package*.json ./

ENV npm_config_ignore_scripts=true \
    npm_config_legacy_peer_deps=true \
    npm_config_audit=false \
    npm_config_fund=false \
    NX_BINARY_SKIP_DOWNLOAD=true \
    NX_NATIVE=false \
    CI=1

RUN (npm ci) || (echo "npm ci failed, fallback to npm install" && npm install)
RUN npm_config_ignore_scripts=false npm rebuild esbuild

COPY ./ /inimatic

RUN test -n "$BUILD_SCRIPT" || (echo "BUILD_SCRIPT is empty" && exit 2) \
 && npm run | grep -Eq "^[[:space:]]+$BUILD_SCRIPT([[:space:]]|$)" \
 || (echo "npm script '$BUILD_SCRIPT' not found. Add it to package.json or pass a correct BUILD_SCRIPT." && exit 3) \
 && npm run "$BUILD_SCRIPT"

RUN test -d /inimatic/www -o -d /inimatic/dist || (echo "No build output (www/ or dist/) found" && exit 4)

FROM nginx:latest
COPY --from=build /inimatic/www /usr/share/nginx/html
RUN rm -f /usr/share/nginx/html/ngsw.json \
    /usr/share/nginx/html/ngsw-worker.js \
    /usr/share/nginx/html/worker-basic.min.js \
    /usr/share/nginx/html/safety-worker.js

COPY ./deployment/nginx/40-generate-runtime-config.sh /docker-entrypoint.d/40-generate-runtime-config.sh
COPY ./deployment/nginx/default.conf /etc/nginx/conf.d/default.conf
RUN chmod +x /docker-entrypoint.d/40-generate-runtime-config.sh
EXPOSE 8080
