# backend.Dockerfile
FROM node:20.18.0
WORKDIR /inimatic_backend
COPY ./package*.json /inimatic_backend
RUN npm install --force
COPY ./backend /inimatic_backend/backend
RUN npm run build:api
EXPOSE 3030
CMD ["npm", "run", "serve:api"]
