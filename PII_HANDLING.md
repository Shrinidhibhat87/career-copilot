# Compliance Handling Playbook

This document explains the reusable design behind this project's PII guardrail. It is a learning guide, not a claim that PII pseudonymization alone provides complete regulatory compliance. A production compliance gateway may also need content policy enforcement, authorization, residency controls, retention rules, audit review, and incident procedures.

## 1. Big picture

The gateway sits between a caller and an LLM provider. It transforms sensitive request text before the provider sees it, keeps the reversal data inside the gateway request lifecycle, and restores the original values only in the model response returned to the caller.

```text
caller
  -> LiteLLM pre-call hook
     -> normalize text-bearing fields
     -> detect language
     -> detect selected PII categories
     -> replace PII and retain a request-scoped mapping
  -> LLM receives pseudonymized text
  -> LiteLLM post-call hook
     -> reverse replacements found in response text
  -> caller receives restored response
```

The main separation of responsibilities is:

- `litellm/pii_guardrail.py`: LiteLLM integration, supported payload shapes, lifecycle hooks, fail-closed boundary, response restoration, audit metadata, and cost accounting.
- `pii_handling/fake_pii.py`: the provider-independent pseudonymization workflow and Azure Language client.
- `pii_handling/language_detection.py`: per-message language and Faker-locale selection.
- `pii_handling/chunking.py`: service-size chunking and exact offset mapping back to original messages.
- `pii_handling/pii_detection_cache.py`: privacy-conscious caching of detection coordinates.

## 2. Trust boundaries and invariants

The most important design rule is that raw PII must not cross the LLM-provider boundary. Enforce these invariants:

1. Every text-bearing request shape is either scanned or explicitly rejected.
2. Detection and pseudonymization finish before the LLM call starts.
3. Any pre-call processing error blocks the request (`PiiRedactionException`); it must never silently forward raw input.
4. The original-to-replacement mapping remains gateway-side and request-scoped.
5. Restoration happens only after the model response returns to the trusted gateway.
6. New endpoint types, tool outputs, image inputs, and file inputs are unsupported until their sensitive content can be inspected safely.

This project deliberately treats cache and audit-log availability differently from redaction:

| Component failure | Behaviour | Reason |
|---|---|---|
| PII detection, payload validation, or replacement | Fail closed; do not call the LLM | Raw PII could escape |
| Dedicated Redis initialization/read/write | Fall back to LiteLLM/in-memory cache or a fresh detection | Caching is an optimization |
| Azure PII logger initialization | Fall back to a local rotating file | Request protection must continue |
| Azure append failure / full logging queue | Drop affected log batch/record and continue | Audit I/O must not block requests |
| PII cost metadata failure | Omit incomplete cost and continue | Accounting is best effort |

Choose these policies explicitly in another system. In particular, do not copy fail-open behaviour for audit infrastructure unless it matches that system's policy.

## 3. Gateway lifecycle

### Pre-call hook

`PiiHandlingGuardrail.async_pre_call_hook` is registered in `config/config.yaml` as a default-on `pre_call` and `post_call` guardrail.

The hook:

1. Removes caller-supplied `pii_cost`, because the gateway owns this value.
2. Resolves `pii_mode` from the active LiteLLM metadata container.
3. Selects the supported content field for the call type.
4. Extracts every scannable string and calls `PiiPseudonymizer.pseudonymize` once.
5. Writes transformed content back into the request.
6. Stores the bidirectional mapping under `secret_fields.pii_info` when a response may need restoration.
7. Records guardrail status, detected-category counts, protected audit information, and best-effort detection cost.

Supported families are:

| LiteLLM call type | Request text | Mapping retained? |
|---|---|---|
| chat completion | `messages[*].content` | Yes |
| text completion | `prompt` | Yes |
| embedding | `input` | No response text to restore |
| Responses API | `instructions`, message content, and supported tool-result text fields | Yes |

Async call names are normalized to the same handlers. Responses API image/file parts and unknown output-bearing item types are rejected because their content is not scanned.

### Post-call hook

`async_post_call_success_hook` reads `secret_fields.pii_info` and restores replacements in:

- chat completion choice text;
- text completion choice text;
- text parts in Responses API output items.

Restoration is literal and longest-replacement-first, avoiding a shorter fake value corrupting a longer one. If the model alters, translates, truncates, or reformats a replacement, exact restoration may not be possible. Placeholder mode is useful when faithful echoing matters.

Streaming relies on LiteLLM invoking the post-call hook for the assembled response. When moving this design elsewhere, verify the framework's streaming lifecycle rather than assuming a normal response hook covers it.

## 4. Core contracts

The useful boundary for reuse is a function shaped like this:

```python
async def pseudonymize(
    content: str | list[str] | list[dict[str, object]],
    cache: DetectionCache | None,
    mode: Literal["fake", "placeholder"],
) -> PseudonymizeResult: ...
```

Conceptual result schema:

```json
{
  "content": "the same structural shape, with selected spans replaced",
  "mapping": {"original sensitive value": "replacement"},
  "detections": [
    {
      "message_index": 0,
      "start": 12,
      "length": 16,
      "text": "original substring",
      "category": "Email"
    }
  ],
  "detected_languages": ["en"]
}
```

`Detection` uses strict, non-negative message indices and offsets. Its `end` is exclusive. Keeping detections in original-message coordinates is what makes replacement safe after batching and overlapping chunks.

Request-level mode contract:

```json
{"metadata": {"pii_mode": "fake"}}
```

`fake` is the default. `placeholder` produces tokens such as `<|EMAIL_1|>`. Missing or null mode selects `fake`; malformed metadata or any other value rejects the request. Responses API requests may use LiteLLM's `litellm_metadata` container instead.

## 5. Detection pipeline

### Normalize and localize

Inputs become an ordered list of strings while preserving their outer structure. Chat message `content: null` is preserved and treated as empty for detection; other non-string content is rejected.

Language is detected per message. Low-confidence, empty, unsupported, or failed detections fall back to English. The resolved language serves two independent purposes:

- Azure PII detection receives a supported language code.
- Faker uses a suitable locale for realistic substitutions.

Keep those lookup tables explicit. A detection provider can support more languages than the fake-data library, so their fallbacks need not match.

### Chunk without losing coordinates

The Azure service accepts limited document sizes and at most five documents per call. Messages of the same language are packed into chunks up to 5,000 characters; long messages are split with a 200-character overlap. Language changes start a new chunk.

Each fragment carries both coordinate systems:

```text
ChunkMetadata(
  message_index,
  chunk_start, chunk_end,
  message_start, message_end
)
```

Azure offsets are mapped back to message offsets. Overlap can produce duplicate or intersecting detections, so ranges are merged before replacement. Replacements are applied from right to left so changing one span's length cannot invalidate later offsets.

### Filter categories deliberately

This project replaces only:

- person;
- address;
- IP address;
- email;
- international bank account number;
- credit card number;
- phone number.

This allowlist is a policy decision, not a universal definition of PII. Review the detection provider's taxonomy, applicable regulations, false-positive tolerance, and domain-specific identifiers before reusing it.

### Person precision: anchor the owner, drop lone tokens

Statistical `PERSON` recognition is the wrong thing to leave as the gate protecting the document owner's identity. It over-fires on technical product names — `Kafka`, `Grafana`, `Apify` — which in a resume are the scoring evidence, and a redacted skill does not error, it just quietly stops counting.

This project therefore runs three layers, in order of authority:

| Layer | Mechanism | Bounded? |
|---|---|---|
| Owner anchor | Exact match of the name, email addresses and phone numbers derived from the resume itself | Yes — finite, and the document states it |
| Single-token drop | A `PERSON` span that is one token is discarded | Yes — structural, nothing to enumerate |
| Term list | Whole-normalized-span exemption for `PERSON` | Escape hatch, small and static |

The owner's identity is derived once, from the resume, and never per call: a scoring prompt is the resume plus a job posting, and a recruiter named in the posting would otherwise compete with the owner for the anchor. Emails and phone numbers come from rule-based recognizers and are taken wholesale, including a referee's — an address is never scoring evidence, so over-reach there costs nothing. The name is inferred, and only from signals that can be checked: a name token appearing in an email local part or in the uploaded filename, proximity to a contact span, and position as a weak tiebreaker. A candidate corroborated by a deterministically found value reports `confirmed`; one that won on position alone reports `likely`; no candidate reports `none`. The level is disclosed in the `/api/resume` payload and in the frontend badge. The derived values never are.

Two narrower rules sit alongside the single-token drop. A multi-token `PERSON` span whose last token is an exempt term is a vendor-qualified product (`Confluent Kafka`, `Azure Databricks`) and is dropped; a name whose *first* token is the product word is not, because that is the shape a real name takes. And a `PHONE` span carrying a decimal point or fewer than seven digits is dropped — no dialable number does either, and Presidio reads a grade like `9.06/10.0 2019` as a phone number. Identity derivation consumes spans these rules have already filtered, so a product name cannot win the owner anchor.

**Stated plainly: third-party single-word names are not redacted.** A mononym, or a recruiter referred to by first name only, survives into the prompt. That is the accepted cost of removing the false-positive class structurally, and the optional local recall pass does not change it — its `PERSON` spans go through the same filter, because it produces the same false positives.

## 6. Replacement and reversal

Two strategies are available:

- **Fake data:** deterministic, locale-aware Faker values preserve natural-looking prompts. The seed derives from message content and language, and collisions are prevented. The tradeoff is that a model may partially copy or modify realistic substitutes.
- **Placeholders:** category counters produce opaque tokens such as `<|PERSON_1|>`. These are easier to restore reliably but may affect model behaviour or reveal the type and count of sensitive entities.

Identical original values share one replacement within a request. A bidirectional map enforces unique replacements, which makes reversal unambiguous. Never persist or expose this map as ordinary request metadata: it contains the original PII and is effectively a decryption key for the pseudonymized prompt.

The current model restores only values present in the request mapping. It does not protect new PII invented or retrieved by the LLM. If that is in scope, add an independent outbound detection/redaction policy before returning the response.

## 7. Detection cache

The cache key is versioned and hashes canonical JSON containing the full message text and resolved language:

```text
pii:detections:v2:<sha256({language, message_content})>
```

The cached value stores only detection offsets, lengths, and categories:

```json
{"detections": [{"start": 12, "length": 16, "category": "Email"}]}
```

On a hit, the original detection text is reconstructed from the current message after strict bounds/type validation. Invalid data becomes a cache miss. Duplicate messages are detected once, and partial hits allow only uncached messages to be sent to Azure.

The default TTL is 600 seconds. Redis is used when configured; otherwise LiteLLM's request cache is used. Although raw text is not stored in cache values, cache keys are deterministic hashes of potentially guessable content. Apply access control, network isolation, TTLs, versioning, and an explicit threat assessment.

## 8. Audit logging and sensitive data

The dedicated PII audit logger records original requests, mappings/detections, and original/restored responses. These records are intentionally sensitive and form a separate high-risk data store.

The implementation uses a bounded non-blocking queue and either daily, host-partitioned Azure append blobs or a local rotating-file fallback. For another project, define before launch:

- who may read these records and for what purpose;
- encryption, residency, retention, deletion, and legal-hold rules;
- whether full raw content is truly required or identifiers/counts would suffice;
- alerting for dropped records and fallback-to-local storage;
- correlation identifiers that do not include raw user content.

Never send raw prompts, mappings, detection text, or restored responses to ordinary application logs, traces, metrics labels, or exception messages.

## 9. Cost and request context

One `LanguageServiceCallContext` is created per gateway request. It sends the authenticated LiteLLM key alias to the language-service proxy through `x-key-alias` and accumulates returned `x-pii-cost` values. A task-local `ContextVar` lets a shared async Azure client attribute concurrent responses safely.

Only a complete, finite, non-negative cost is written to `spend_logs_metadata.pii_cost`, exposed as `x-litellm-pii-cost`, and added once to LiteLLM's response cost. This mechanism is useful but tightly coupled to LiteLLM internals; pin it with contract tests whenever LiteLLM is upgraded.

## 10. Applying the pattern to another project

Use this sequence:

1. Inventory every LLM entry point and every text-bearing payload shape, including tool outputs, streaming, retries, batch APIs, images, and files.
2. Write the trust boundary and failure matrix before implementation.
3. Define a strict normalized-content and detection contract independent of the gateway framework.
4. Select a detection provider and document supported languages, categories, size limits, timeouts, and error semantics.
5. Implement coordinate-preserving chunking, overlap deduplication, and right-to-left replacement.
6. Keep reversal mappings request-scoped and inside trusted infrastructure.
7. Integrate at the last hook before provider dispatch and the first hook after the complete response.
8. Reject any unhandled content shape until it has an explicit scanning path.
9. Add caching and cost tracking only after the safety path is correct.
10. Treat audit storage as a PII system with its own access and retention controls.

Minimum test matrix:

- each supported endpoint and sync/async variant;
- empty, null, malformed, multilingual, repeated, and oversized content;
- detections at chunk boundaries and overlapping detections;
- every allowed category and both replacement modes;
- replacement collision and longest-first restoration;
- provider timeout/error, batch mismatch, invalid cache entry, and cache outage;
- unsupported call types, content parts, tool-result fields, images, and files fail closed;
- concurrent requests do not share mappings or cost context;
- streaming responses are restored before any bytes reach the caller;
- logs and normal telemetry do not leak sensitive bodies;
- dependency-upgrade contract tests for framework hook order and billing internals.

## 10a. Local models are inside the boundary, not outside it

A role served by Ollama or vLLM is redacted exactly as a role served by OpenRouter. The
loopback argument — nothing leaves the machine, so redaction is wasted work — is refused
on purpose. The invariant this gateway defends is that a prompt carries no identity, and
that invariant has to hold wherever the prompt goes, because where it goes is one line in
`models.roles` and a line can be edited. A build in which local prompts are raw is a build
where changing a backend silently changes what leaves the machine.

`pii.enabled: false` remains the single, loud way out, and it warns at run start.

One local-specific consequence points the other way: `pii.llm_pass`, the optional Ollama
recall pass, now shares the runtime scoring already uses, so on a machine with Ollama up
it costs a model that is loaded anyway.

## 11. Configuration checklist

Relevant settings in this project include:

| Setting | Purpose / default |
|---|---|
| `LANGUAGE_SERVER_HOST` | Required PII service host |
| `LANGUAGE_SERVER_PORT` | PII service port, default `5000` |
| `LANGUAGE_SERVER_KEY` | PII service/proxy key |
| `LANGUAGE_SERVER_CONNECTION_TIMEOUT` | Connect timeout, default `5s` |
| `LANGUAGE_SERVER_READ_TIMEOUT` | Read timeout, default `5s` |
| `LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD` | Language fallback threshold, default `0.5` |
| `PII_DETECTIONS_CACHE_TTL_SECONDS` | Detection-cache TTL, default `600s` |
| `REDIS_URL` or `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD` | Dedicated detection cache |
| `PII_LOG_FILE` | Local/fallback audit-log path |
| `PII_AZURE_BLOB_ACCOUNT_URL`, `PII_AZURE_BLOB_CONTAINER`, `PII_AZURE_BLOB_PREFIX` | Protected Azure audit sink |

Before production, validate end to end that the provider receives only pseudonymized content and the caller receives correctly restored content. Unit tests alone cannot prove hook wiring, streaming behaviour, deployment configuration, or network routing.