# Multi-stage build for minimal production image.
# Stage 1: Build the Go binary
FROM golang:1.23-alpine AS builder

WORKDIR /app

# Copy go module files first for layer caching
COPY go.mod go.sum* ./
RUN go mod download 2>/dev/null || true

# Copy source code
COPY . .

# Download dependencies (in case go.sum doesn't exist yet)
RUN go mod tidy

# Build static binary with CGO disabled
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /arcfield ./cmd/server

# Stage 2: Minimal runtime image
FROM alpine:3.20

RUN apk --no-cache add ca-certificates tzdata

WORKDIR /app

COPY --from=builder /arcfield .

EXPOSE 8080

ENTRYPOINT ["/app/arcfield"]
