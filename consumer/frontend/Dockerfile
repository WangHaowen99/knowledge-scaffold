# Multi-stage build for React frontend

# Build stage
FROM node:20-alpine AS builder

WORKDIR /app

RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.aliyun.com/g' /etc/apk/repositories

# Copy package files
COPY package*.json ./

# Install dependencies
RUN npm ci --registry=https://registry.npmmirror.com

# Copy source code
COPY . .

# Build the application
RUN npm run build

# Production stage
FROM nginx:alpine

RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.aliyun.com/g' /etc/apk/repositories

# Copy built assets from builder stage
COPY --from=builder /app/dist /usr/share/nginx/html

# Copy nginx configuration
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Runtime env replacement before nginx starts
COPY docker-entrypoint.d/99-runtime-env.sh /docker-entrypoint.d/99-runtime-env.sh
RUN sed -i 's/\r$//' /docker-entrypoint.d/99-runtime-env.sh \
    && chmod +x /docker-entrypoint.d/99-runtime-env.sh

# Expose port 80
EXPOSE 80

# Start nginx
CMD ["nginx", "-g", "daemon off;"]
