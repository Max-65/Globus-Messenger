FROM golang:1.25-alpine AS builder
RUN apk add --no-cache gcc musl-dev
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY main.go .
RUN CGO_ENABLED=1 GOOS=linux go build -o main .

FROM alpine:latest
RUN apk add --no-cache sqlite
WORKDIR /app
COPY --from=builder /app/main .
EXPOSE 8081
CMD ["./main"]