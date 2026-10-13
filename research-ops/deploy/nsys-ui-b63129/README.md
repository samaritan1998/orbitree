# Nsight Systems UI for b63129

This container runs the native Nsight Systems 2026.2.1 Linux UI in Xvfb and
exposes it through noVNC. The profile directory is mounted read-only.

```bash
docker build -t nsys-ui-b63129:2026.2.1 deploy/nsys-ui-b63129

docker run -d \
  --name nsys-ui-b63129 \
  --restart unless-stopped \
  -p 8770:6080 \
  -e DEFAULT_RANK=0 \
  -v /data/models/experiments/demo-profiling-job/nsys/demo-profiling-job-b63129:/reports:ro \
  nsys-ui-b63129:2026.2.1
```

Open `http://HOST:8770/vnc.html?autoconnect=1&resize=scale`.

The UI starts with rank 0. Use **File > Open** and select another file from
`/rank-reports` to inspect ranks 1-31.
