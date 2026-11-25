FROM node:22-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY app app
COPY tsconfig.json .
RUN npm run build
CMD ["npm", "start"]
