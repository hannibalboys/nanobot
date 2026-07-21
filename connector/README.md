# nanobot-connector

A lightweight daemon that runs on **your computer** and lets a remote nanobot
server read an **allow-listed, read-only** slice of your local files — without
uploading them first. It connects **outbound** to the server (reverse-connect,
like a CMDB agent), so your computer needs no public IP and no open ports.

## Why

When nanobot runs on a server (e.g. `192.168.90.100`), the agent can only see
the server's filesystem. If your source material lives on your own PC, the agent
can't use it. The connector bridges that gap on demand: the agent lists, searches,
reads, and fetches files from folders you explicitly share.

## Install

Download the single-file executable for your OS from the Releases page, or from
source:

```bash
pip install -e .
```

## Usage

```bash
# 1. Pair using a code generated in the nanobot WebUI (Devices page)
nanobot-connector pair --server wss://192.168.90.100:8765 --code AB3F7K2Q

# Self-signed server certificate? Pin its fingerprint:
nanobot-connector pair --server wss://192.168.90.100:8765 --code AB3F7K2Q \
                       --fingerprint <sha256-of-server-cert>

# 2. Share one or more folders (read-only)
nanobot-connector allow "D:\PPT materials"

# 3. Run it
nanobot-connector start            # foreground
nanobot-connector service install  # or start on boot

# Inspect
nanobot-connector status
```

## Security

- **Read-only.** v1 exposes only list/search/read/fetch — no writes, no command execution.
- **Allow-list.** Only folders you add with `allow` are visible; `..` and symlink
  escapes are rejected; filesystem roots can't be shared.
- **TLS.** Full certificate verification by default; `--fingerprint` pins a
  self-signed cert; `--insecure` is explicit and discouraged.
- **Outbound only.** The connector never listens on a port.
- **Audit.** Every file access is logged to `~/.nanobot-connector/logs/audit.log`.

## Development

```bash
pip install -e ".[dev]"
pytest -q
ruff check nanobot_connector
pyinstaller packaging/nanobot-connector.spec
```
