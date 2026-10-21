# ocpp-mitm

A logging proxy that sits between an OCPP charger and its controller (CSMS). It
relays every message unchanged, except for the rewrites you ask for, and logs
each one to the terminal and to a JSONL file. Calls are paired with their
results, with latency.

It handles both transports on one port:

- **OCPP-J** (1.6J, 2.0.1, 2.1): a WebSocket upgrade is relayed to
  `--upstream` plus the charger's path (usually `/<chargePointId>`). The
  subprotocol and `Authorization` header pass through, and the charger gets the
  controller's real handshake answer, including a 401.
- **OCPP SOAP** (1.5, 1.6S): a POST is relayed to the controller. The charger's
  advertised callback URL in `wsa:From` is replaced with the proxy's callback
  listener, so the controller's own calls (Reset, RemoteStart, ...) come back
  through the proxy. They go on to the charger's real URL, with `wsa:To` set
  back to it.

## Run

```sh
uv run ocpp-mitm --upstream wss://csms.example.com/ocpp
```

Then set the charger's controller URL to `ws://<laptop IP>:9000/` (or
`http://<laptop IP>:9000/` for SOAP). The startup banner prints the laptop's
LAN IP.

| Flag | Purpose |
| --- | --- |
| `--listen HOST:PORT` | charger-facing listener, default `0.0.0.0:9000` |
| `--cert` / `--key` | serve `wss://`/`https://` to the charger |
| `--callback-listen HOST:PORT` | SOAP callbacks from the controller, default `0.0.0.0:9001` |
| `--callback-url URL` | callback URL put in `wsa:From`; default `http://<LAN IP>:9001` |
| `--rewrite CHARGER=CONTROLLER` | literal swap: `CHARGER` → `CONTROLLER` towards the controller, reverse towards the charger; repeatable |
| `--insecure` | skip verifying the controller's TLS certificate |
| `--log PATH` | JSONL file, default `./ocpp-<timestamp>.jsonl` |
| `--full` | do not truncate messages on the terminal |

The proxy never verifies the charger's certificate on SOAP callbacks, because
chargers almost always present self-signed ones.

### TLS towards the charger

If the charger only connects over `wss://`/`https://`, it has to trust the
proxy's certificate. Make one for the laptop's IP and load its CA onto the
charger (security profile 2 chargers usually accept an uploaded CA):

```sh
mkcert -cert-file proxy.pem -key-file proxy-key.pem 192.168.1.10
uv run ocpp-mitm --upstream wss://csms.example.com/ocpp --cert proxy.pem --key proxy-key.pem
```

A charger that pins the controller's certificate, or uses client certificates
(security profile 3), cannot be intercepted this way.

## Log format

Each line is one event: `ts`, `proto` (`ocpp-j` or `soap`), `cp`, `dir`
(`cp->csms` or `csms->cp`), `kind` (`call`, `result`, `error`, `conn`, ...),
`action`, `id`, `latency_ms`. It also holds the original message (`frame` or
`body`) and, if a rewrite changed it, the `forwarded` text.

```sh
jq 'select(.action == "StartTransaction")' ocpp-*.jsonl
```

## Limits

- The SOAP charger-URL map lives in memory. After a restart, the controller's
  calls get a 502 until the charger sends a request (its next Heartbeat).
- `--rewrite` is a plain substring swap over the whole message.

## Develop

```sh
uv run pytest
```
