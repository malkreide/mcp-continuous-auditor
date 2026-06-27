# deploy/ — Phase-5 rollout kit

Runnable scripts to actually roll out Phase 5 on a host you control. They run **on
the host VM**, not in a Claude session — nothing here can be exercised from the
auditor's cloud sandbox. Full step-by-step:
[`docs/deployment/phase5-rollout.md`](../docs/deployment/phase5-rollout.md).

```
00-preflight.sh              host readiness: nested KVM, qemu, socat, docker, vsock
tensorzero/
  up.sh                      bring up gateway + ClickHouse, healthcheck + smoke test
  episode-tokens.sh          sum one run's tokens from ClickHouse (per-run cost-cap)
microvm/
  build-worker-image.sh      download base cloud image + render cloud-init seed
  run-worker.sh              boot ONE throwaway Worker microVM, run audit, discard
  worker-cloud-init.yaml.tmpl in-guest bootstrap (no credentials; vsock-only egress)
  channel/
    broker-listener.sh       host side: receive results over vsock into a dropbox
```

Topology (single local Linux VM): the **Broker** is the host VM (OpenClaw +
credentials + TensorZero gateway, all on localhost); the **Worker** is a
throwaway microVM per run that reads the target read-only and ships only the
deterministic result back over vsock. The Worker never holds a credential.

Generated artifacts (base images, overlays, seeds, the listener's per-connection
handler) land in `deploy/.images/` and are gitignored. Secrets come from `.env` /
the environment, never inline.
