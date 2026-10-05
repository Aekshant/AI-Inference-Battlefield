# Binary is built on the host (make build) to reuse the local Go module cache.
FROM gcr.io/distroless/static-debian12:nonroot
COPY bin/gateway /gateway
EXPOSE 8080
ENTRYPOINT ["/gateway"]
