// Deterministic assertions shared by promptfooconfig.determ.yaml and
// promptfooconfig.yaml. Referenced as `file://assertions/contract.js:<name>`.
//
// WHY THESE ARE FUNCTIONS IN A FILE. The inline versions they replace failed
// in two ways, both silently:
//
//   * A multi-line inline value is used by promptfoo as a FUNCTION BODY, so an
//     `(() => { … })()` written over several lines evaluates and returns
//     nothing — "Custom function must return a boolean", on every run.
//   * They read the output and nothing else. "The payload was not forwarded"
//     was asserted as "the output does not contain it", and an argument-
//     validation error that never reached the tool satisfied that perfectly.
//
// Every function here returns a GradingResult with a reason, so a failure says
// what was seen rather than only that something was false.

'use strict';

const FENCED_JSON = /```json\s*\n([\s\S]*?)\n```/;

function result(pass, reason) {
  return { pass, score: pass ? 1 : 0, reason };
}

function isErrorText(output) {
  return /^\s*(Fehler|Error)\b/i.test(String(output));
}

function argsOf(context) {
  try {
    return JSON.parse((context && context.vars && context.vars.args) || '{}');
  } catch (e) {
    return {};
  }
}

// The upstream requests the tool made, as recorded by providers/call_tool.py.
// `null` means the provider sent no log — which is "not measured", never "none".
function requestsOf(context) {
  const meta = context && context.providerResponse && context.providerResponse.metadata;
  return meta && Array.isArray(meta.requests) ? meta.requests : null;
}

// zurich_datastore_sql returns Markdown with the records in one fenced JSON
// block — it has since its first commit; it has never returned bare JSON. This
// is the contract it actually has: the block parses, it is an array of
// objects, and it respects the LIMIT the query asked for.
function datastoreRows(output, context) {
  if (isErrorText(output)) return result(false, `tool returned an error: ${String(output).slice(0, 160)}`);
  const m = FENCED_JSON.exec(String(output));
  if (!m) return result(false, 'no fenced ```json block of records in the output');
  let rows;
  try {
    rows = JSON.parse(m[1]);
  } catch (e) {
    return result(false, `the records block is not valid JSON: ${e.message}`);
  }
  if (!Array.isArray(rows)) return result(false, 'the records block is not an array');
  const bad = rows.findIndex((r) => r === null || typeof r !== 'object' || Array.isArray(r));
  if (bad !== -1) return result(false, `record ${bad} is not an object`);
  const sql = ((argsOf(context).params || {}).sql) || '';
  const lim = /\bLIMIT\s+(\d+)/i.exec(sql);
  if (lim && rows.length > Number(lim[1])) {
    return result(false, `${rows.length} records for LIMIT ${lim[1]}`);
  }
  return result(true, `${rows.length} well-formed records${lim ? ` (LIMIT ${lim[1]})` : ''}`);
}

// Zero rows is a result, not an error, and it carries no records.
function datastoreEmpty(output) {
  if (isErrorText(output)) return result(false, `tool returned an error: ${String(output).slice(0, 160)}`);
  const m = FENCED_JSON.exec(String(output));
  if (m) {
    try {
      const rows = JSON.parse(m[1]);
      if (Array.isArray(rows) && rows.length > 0) return result(false, `${rows.length} records in an "empty" result`);
    } catch (e) {
      return result(false, `a records block that is not valid JSON: ${e.message}`);
    }
  }
  return result(true, 'empty result reported without records and without an error');
}

function notAnError(output) {
  return isErrorText(output)
    ? result(false, `tool returned an error: ${String(output).slice(0, 160)}`)
    : result(true, 'tool returned a result, not an error');
}

// A rejected statement must not leave the server at all.
function notForwarded(output, context) {
  const reqs = requestsOf(context);
  if (reqs === null) return result(false, 'the provider recorded no request log — forwarding was not measured');
  return reqs.length === 0
    ? result(true, 'no upstream request was made')
    : result(false, `${reqs.length} upstream request(s) made: ${JSON.stringify(reqs).slice(0, 300)}`);
}

// The query reached upstream, and every single quote in it arrived doubled —
// i.e. inside the SQL string literal, not breaking out of it.
function quoteDoubledUpstream(output, context) {
  const reqs = requestsOf(context);
  if (reqs === null) return result(false, 'the provider recorded no request log — escaping was not measured');
  const raw = ((argsOf(context).params || {}).query) || '';
  if (!raw.includes("'")) return result(false, 'the test query contains no single quote — nothing to measure');
  const escaped = raw.replace(/'/g, "''");
  const sent = reqs.map((r) => JSON.stringify(r)).join('\n');
  if (!sent.includes(escaped)) {
    return result(false, `the escaped query never reached upstream: ${sent.slice(0, 300) || '(no request)'}`);
  }
  if (sent.split(escaped).join('').includes(raw)) {
    return result(false, `the query was also sent unescaped: ${sent.slice(0, 300)}`);
  }
  return result(true, `sent upstream only as ${escaped}`);
}

module.exports = {
  datastoreRows,
  datastoreEmpty,
  notAnError,
  notForwarded,
  quoteDoubledUpstream,
};
