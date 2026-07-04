# Forward proxy — domain-level egress allowlist (Analysis S-C)

The nft rulesets (`egress-allowlist.nft`, `broker-egress-allowlist.nft`) enforce a
**port + LAN + fixed-DNS-resolver** allowlist. They deliberately do **not** restrict
*which* web domains port 443 reaches — stateless nft cannot match a CDN's shifting
IPs by domain. This is the tier that does: a tinyproxy with a **default-deny domain
allowlist**, so egress is limited to named hosts (GitHub, PyPI, npm, Debian,
Zürich for the Worker; the LLM/grader, GitHub and Telegram APIs for the Broker).

Until this proxy is in place, HTTPS exfil to an arbitrary host is still possible
over the nft `tcp dport 443 accept` rule (the Worker holds no credentials and the
target is public, so the residual risk is a pivot/C2 channel, not credential loss —
but the Broker side does hold credentials, so run the proxy there first).

## Files

- `tinyproxy.conf.tmpl` — the proxy config template (`${ALLOWLIST}` → an allow file)
- `worker-allow.txt` — domains the Worker legitimately needs
- `broker-allow.txt` — domains the Broker legitimately needs

## Run it (Broker side — highest value, it holds the credentials)

```bash
sudo apt-get install -y tinyproxy
ALLOWLIST="$PWD/deploy/microvm/forward-proxy/broker-allow.txt" \
  envsubst < deploy/microvm/forward-proxy/tinyproxy.conf.tmpl > /etc/tinyproxy/tinyproxy.conf
sudo systemctl restart tinyproxy

# Point the Broker / OpenClaw process at it (local):
export https_proxy=http://127.0.0.1:8888  http_proxy=http://127.0.0.1:8888
```

## Run it (Worker side)

Bind a second tinyproxy instance (port 8889) with `worker-allow.txt`. With QEMU
SLIRP the guest reaches the host loopback as **10.0.2.2**, so set in the Worker
cloud-init (or its shell env):

```bash
export https_proxy=http://10.0.2.2:8889  http_proxy=http://10.0.2.2:8889
# apt honours its own key, add:  echo 'Acquire::http::Proxy "http://10.0.2.2:8889";' > /etc/apt/apt.conf.d/01proxy
```

git, uv/pip and npm all honour `http(s)_proxy`; apt needs the `Acquire::*::Proxy`
key above. Verify a denied host is refused (e.g. `curl https://example.com` → 403)
before trusting the allowlist.

## Trade-offs

- tinyproxy filters the **CONNECT host** for HTTPS, not the TLS SNI, so it trusts
  the client-supplied host — fine for a cooperating client, weaker against a fully
  hostile in-VM process. For stronger enforcement use a TLS-terminating/SNI-aware
  proxy (squid with `ssl_bump peek`, or mitmproxy) — heavier, documented as the
  next step.
- Keep the allowlists minimal and reviewed; adding a domain is a deliberate change.
