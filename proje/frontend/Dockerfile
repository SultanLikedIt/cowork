# Stage 1: build the Vite/React static bundle. This is the one step in the
# whole Docker setup that needs network access (npm install) -- there is no
# offline npm bundle equivalent to the backend's wheel bundle. It only runs
# at image-build time, never at container runtime.
FROM node:20-slim AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

# Stage 2: serve the static build + reverse-proxy /api to the backend.
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
