# TensorZero-Gateway (Phase 5) — Cost-Caps, A/B & Audit-Trail

> Status: optional · Config: [`tensorzero/tensorzero.toml`](../../tensorzero/tensorzero.toml)
> · Stack: [`tensorzero/docker-compose.yml`](../../tensorzero/docker-compose.yml)

TensorZero ist ein LLM-Gateway, das **zwischen OpenClaw und den Provider**
(Anthropic) gesetzt wird. Statt dass OpenClaw das Modell direkt aufruft, geht
jeder Aufruf durch das Gateway. Das bringt drei Dinge, die Phase 5 fordert:

1. **Cost-Caps pro Cron-Lauf** — harte `max_tokens` pro Aufruf (provider-seitig,
   *bevor* Tokens entstehen) plus ein gemessener Token-/Kosten-Trail pro Lauf,
   den `budget_guard.py` als hartes Pro-Lauf-Ceiling erzwingt.
2. **A/B** — denselben Audit-Turn über zwei Varianten (z. B. Opus vs. Sonnet)
   splitten und messen, ob das günstigere Modell die Audit-Qualität hält, bevor
   man umstellt.
3. **Audit-Trail** — jeder Modellaufruf (Tokens, Kosten, Variante, Episode) landet
   in ClickHouse und ist später abfragbar. Nachvollziehbarkeit statt Blindflug.

Das ergänzt die Budget-Leitplanken
([../budget/guardrails.md](../budget/guardrails.md)): TensorZero ist die
**provider-seitige** Schranke + Messung, `budget_guard.py` der **lauf-seitige**
Circuit Breaker. Zusammen: Caps pro Aufruf (Gateway) → Caps pro Lauf (Guard) →
Skip nach wiederholtem Versagen (Breaker).

## Topologie

```
OpenClaw ──(provider baseURL)──▶ TensorZero-Gateway ──▶ Anthropic API
                                      │
                                      ▼
                                  ClickHouse  (Tokens/Kosten/Variante je Episode)
```

Der Anthropic-Key liegt **nur** im Gateway-Prozess (`docker-compose.yml`:
`ANTHROPIC_API_KEY` in der Gateway-`environment`), nicht in OpenClaw und nicht in
`tensorzero.toml`. Auf dem dedizierten Host läuft das Gateway lokal
(`127.0.0.1:3000`), erreichbar nur vom OpenClaw-Prozess.

> Zusammenspiel mit forkd (Phase 5): In der Zwei-VM-Topologie
> ([../deployment/forkd-isolation.md](../deployment/forkd-isolation.md)) läuft das
> Gateway in der **Broker-VM** (sie hält ohnehin den Provider-Key). Die
> Worker-VM ruft kein Modell direkt.

## Starten

```bash
cd tensorzero
cp ../.env.example .env        # ANTHROPIC_API_KEY, CLICKHOUSE_PASSWORD ausfüllen
docker compose up -d
# Smoke-Test (OpenAI-kompatibler Endpunkt des Gateways):
curl -s http://127.0.0.1:3000/openai/v1/models | head
```

Bilder vorher auf eine getestete Version pinnen — `:latest` driftet, und die
Config-Schlüssel wandern zwischen Releases (deshalb sind die Templates als
„prüfen gegen deine Version" markiert).

## OpenClaw auf das Gateway zeigen

OpenClaw soll den Audit nicht mehr direkt an Anthropic schicken, sondern an das
Gateway. Zwei übliche Wege (je nach OpenClaw-Version):

- **Provider-`baseURL` überschreiben** auf `http://127.0.0.1:3000/openai/v1`
  (TensorZero spricht einen OpenAI-kompatiblen Endpunkt), Modellname = der
  TensorZero-**Function**-Name (`nightly_audit`), nicht der rohe Provider-Name.
- Oder, falls OpenClaw `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY` aus dem Env
  liest: `ANTHROPIC_BASE_URL=http://127.0.0.1:3000` setzen und einen
  Dummy-Key, da der echte Key im Gateway sitzt.

Der Cron-Payload (`openclaw/cron/nightly-audit.json`) bleibt strukturell gleich:
`model` zeigt dann auf die TensorZero-Function, `fallbacks: []` bleibt, damit ein
nicht auflösbarer Pfad weiterhin **hart fehlschlägt** statt still auszuweichen
(Plan Phase 4). Provider-seitige Fallbacks/Retries gehören in `tensorzero.toml`,
nicht in die stille Modell-Degradierung von OpenClaw.

## Cost-Cap pro Cron-Lauf (Ende-zu-Ende)

1. `nightly-audit.sh` erzeugt pro Lauf eine **Episode-ID** und gibt sie als
   `TENSORZERO_EPISODE_ID` an die Aufrufe weiter. Alle Modellaufrufe dieses Laufs
   (Agent + Grader) tragen dieselbe ID.
2. Das Gateway schreibt Tokens + Kosten je Aufruf nach ClickHouse, getaggt mit
   der Episode-ID.
3. Nach dem Lauf summierst du die Tokens dieser Episode (ClickHouse-Query) und
   reichst sie an den Guard:

   ```bash
   python3 scripts/budget_guard.py record \
     --exit-code "${outcome_rc}" --tokens "${episode_tokens}"
   ```

   Das ersetzt die promptfoo-nur-Zählung durch den **vollständigen** Cost-Trail
   über alle Modellaufrufe. Überschreitet eine Episode `BUDGET_TOKENS_PER_RUN`,
   kippt der Breaker — der nächste Lauf wird übersprungen.

Beispiel-Query (Schema je TensorZero-Version prüfen):

```sql
SELECT sum(input_tokens + output_tokens) AS total_tokens, sum(cost_usd) AS cost
FROM tensorzero.ModelInference
WHERE episode_id = {episode_id:String};
```

## A/B auswerten

`tensorzero.toml` splittet `nightly_audit` 50/50 auf `baseline` (Opus) und
`candidate` (Sonnet). Über ClickHouse vergleichst du pro Variante: Kosten,
Latenz und — der eigentliche Punkt — ob die deterministischen Gates
(`outcome` aus `nightly-summary.json`) gleich ausfallen. **Wahrheitsinstanz
bleibt die CI**, nicht das Modell: A/B misst nur, welche Variante dieselben
deterministischen Ergebnisse günstiger erreicht. Erst wenn die günstigere
Variante über genug Läufe identisch urteilt, verschiebst du das `weight`.

## Audit-Trail

ClickHouse hält pro Inferenz: Function, Variante, Modell, Input/Output-Tokens,
Kosten, Latenz, Episode-ID und die (untrusted zu behandelnden) Prompts/Outputs.
Damit lässt sich jeder nächtliche Lauf rekonstruieren — welche Variante lief, was
sie kostete, und ob ihr Urteil mit dem deterministischen Gate übereinstimmte.

## Grenzen / „erst wenn stabil"

- Zusätzliche bewegliche Teile (Gateway + ClickHouse). Auf dem Pi: ClickHouse
  braucht RAM/IO — eher auf die NVMe-SSD legen oder ClickHouse auf einen anderen
  Host auslagern.
- Optional und additiv: Solange du direkt gegen Anthropic fährst, greifen die
  Pro-Lauf-Caps weiter über `budget_guard.py` (promptfoo-Tokens). TensorZero
  hebt nur die Genauigkeit des Cost-Trails und schaltet A/B frei.
