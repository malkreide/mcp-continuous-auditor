# The probes

One page per probe. The README lists what each one asks in three lines; this is
where the case histories live — the incident that produced the check, what the
finding is and is not allowed to claim, and the flags that matter.

Every probe here is deterministic. You run it and read the answer; you do not
ask a model what it thinks. And every one of them exists because a server was
green and wrong at the same time.

| Probe | The question it asks | Page |
|---|---|---|
| identity | Does the server report the version it actually is? | [identity.md](identity.md) |
| shipped | Is the release users install current — and does it run? | [shipped.md](shipped.md) |
| yank | Is a known-broken release still installable? | [yank.md](yank.md) |
| published | What does the *installed* artifact do on the wire? | [published.md](published.md) |
| lockfile | Is the declared bound in force where the install happens? | [lockfile.md](lockfile.md) |
| doc-claim | Do the identifiers the documentation cites exist? | [doc-claim.md](doc-claim.md) |
| parity | Does the translated documentation still say the same thing? | [parity.md](parity.md) |
| provenance | Which commit is this report about? | [provenance.md](provenance.md) |
| pr-health | Which open pull requests are not green — including the ones that are not red either? | [pr-health.md](pr-health.md) |

Three more gates run inside the nightly audit rather than as standalone probes,
and are documented at the top of their own files:

* `scripts/transport_boot_probe.py` — does the server come up on the transports
  it declares, and does it answer `initialize` + `tools/list`?
* `scripts/rebind_probe.py` — is the inbound host allow-list actually enforced?
* `scripts/live_probe.py` and `scripts/recall_canary.py` — does the live endpoint
  still return the shape *and the volume* it used to? The first checks the
  endpoint, the second the server's own tools, which is the whole chain.

## The shared rule

Every one of these reports distinguishes three answers, never two:

* **clean** — the check ran and found nothing,
* **finding** — the check ran and found something,
* **not measured** — the check did not run, or could not conclude.

The third is the one that takes discipline. A probe that cannot resolve a value
and reports "OK" has told you something false about a state it never read, and
that mistake is invisible in exactly the way the defects here are. Every exit
code in this directory keeps the three apart, and every "UNVERIFIED",
"NOT MEASURED", "UNCONFIRMED" and "MOVED_DURING_RUN" in the output is that rule
being applied.
