FROM node:22-alpine AS base
WORKDIR /app
COPY apps/web/package.json /app/package.json
COPY apps/web/package-lock.json /app/package-lock.json
RUN npm install
COPY apps/web /app
EXPOSE 3000

FROM base AS development
ENV API_INTERNAL_URL=http://api:8000
CMD ["npm", "run", "dev"]

FROM base AS production
RUN npm run build
CMD ["sh", "-c", "npx next start -H 0.0.0.0 -p ${PORT:-3000}"]
