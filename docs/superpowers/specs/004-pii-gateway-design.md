# PII Gateway — Design

> A redaction boundary in front of every LLM provider call. The resume owner's name, email and phone are replaced by placeholders before text leaves the machine and restored when the reply comes back. Core rule: fail closed. If text cannot be scanned and redacted, the provider is not called. Builds on the `LLMClient` port (001-foundation), the stored CV (010-persistence-and-lifecycle) and the Profile page (003-frontend-onboarding).

## Decisions

| Topic | Decision |
|---|---|
| Protects | The user's direct contact identity: name, email, phone, street address, URLs, IBAN, card number, IP. It stops these reaching, being logged by, or being trained on by the provider. |
| Does not protect | A compromised local machine. Identity inferred from remaining text. Anything the model invents. Retention, residency or legal questions. |
| Pseudonymous, not anonymous | Employers, schools and dates stay in the prompt. They are scoring evidence, and match quotes from the resume must stay verbatim. The UI never says "anonymous". |
| Entities | Configuration, not code: `pii.entities` picks from the eight known categories. Anything else is rejected as an unknown category. |
| Placement | `RedactingLLMClient` decorates `LLMClient`. It sits in front of the litellm client, so the retry loop inside re-sends an already-redacted conversation. No proxy, no extra process. |
| Where it is built | `build_redactor` in `adapters/pii/factory.py`. Used by the pipeline, the API (`app.py`, `intent.py`) and `doctor`. |
| Off switch | `pii.enabled: false` returns no redactor and logs a warning. An omitted `pii:` block keeps redaction on. |
| Floor | Presidio, CPU only, always on. Email, IBAN, credit card, IP, URL and phone are rule-based and exact. `PERSON` is spaCy NER (`en_core_web_lg`), which is statistical and can miss. |
| Street address | A dedicated pattern recognizer. `LOCATION` is not mapped to `ADDRESS`: cities such as Berlin drive filters and scoring and must survive. |
| Recall pass | Off by default (`llm_pass: false`). When on, a local Ollama model proposes spans, merged with Presidio's by `HybridDetector`. Every proposed span must be a literal substring of the input or it is dropped. The model never gives offsets. |
| Local only | `llm_endpoint` must be loopback unless `llm_allow_remote: true`. Readiness (`check_ready`, model present) is checked when the redactor is built, not mid-run. |
| Recall failure | `llm_failure: block` aborts. `presidio_only` falls back to the floor and logs a warning. |
| Fail closed | Any pre-call error blocks the call: detector failure, malformed span, non-text prompt or system, unscannable shape. `PiiRedactionError` is an `LLMCallError`, so the run aborts with its spend recorded. A missing spaCy model raises `PiiModelMissingError`, which names the model. |
| Spans | Validated before use: start >= 0, length > 0, end within the text, known category, one result list per input. Strictly overlapping spans merge (widest wins). Touching spans stay separate. Replacement runs right to left. |
| Placeholders | `<\|CATEGORY_N\|>`, numbered per category by first appearance. One value gets one token within a request. Not fake data, so a mangled token fails loudly instead of restoring wrongly. |
| Map lifetime | The original-to-placeholder map is a local in `complete()`. Never an attribute, never stored, never logged, never on `LLMResponse`. |
| Restore | Longest placeholder first, over `str`, `list` and `dict`. Restoration also runs inside the caller's validator, so the verbatim-quote check sees original text. `prompt_hash` hashes the original prompt. |
| Residue | After restoring, any leftover `<\|` raises `PiiRedactionError`. A placeholder the model omits is fine. A model that drops the delimiters is not caught; the quote check rejects it. |
| Outbound scan | `pii.outbound`: `allow`, `warn` (default, logs category and count only), `block`. Values the gateway itself substituted are not flagged. A scanner failure logs and returns the reply. It is a monitor, not a gate. |
| Logging | No mapping value, prompt text, restored text or placeholder in any log or error. Category counts are allowed. |
| Language | `pii.language` is `en`. Rule-based detectors are language-independent. NER recall is lower on German text, so a German recruiter's name may pass. The recall pass is multilingual. |
| Owner anchor | The owner's identity is derived from the CV text, not typed in. It is learned once per run from the stored CV (`Redactor.learn`) and on upload from the file text and filename. It is redacted everywhere it occurs. |
| Contacts | Every `EMAIL` and `PHONE` span in the CV is an owner contact, wherever it sits. Over-reach on contacts costs nothing. |
| Name | Candidates are all multi-token `PERSON` spans. Scores: email local part contains a name token of 3+ chars (+3), filename does (+2), within 200 chars of a contact (+2), earliest `PERSON` (+1). Highest wins, ties go to the earliest. |
| Confidence | `confirmed`: the email or filename signal matched. `likely`: won on proximity or position only. `none`: no candidate. Position alone can never confirm. |
| At `none` | Emails and phones still anchor. The name falls back to NER. The single-token drop is suspended within 200 chars of a contact span. |
| Learn from filtered spans | Identity is derived after the precision filter, so `Confluent Kafka` cannot win the name and a grade-and-year run cannot become a phone. |
| Single-token drop | A one-word `PERSON` span is dropped (`drop_single_token_person`). Kafka, Grafana, Apify are skills, not people. The owner is covered by the anchor. |
| Term exemption | A `PERSON` span equal to an exempt term (whole normalized text) is dropped. A multi-token span whose last token is exempt is a product (`Apache Kafka`); `Kafka Rodrigues` is still redacted. Seed: the preferences' `core_skills`, `_PRODUCT_TERMS` in `person_policy.py`, and `pii.person_false_positive_terms`. |
| Fake phones | A `PHONE` span is dropped when it has a decimal point or fewer than 7 digits. No other non-`PERSON` category is ever suppressed. |
| Order | In `Redactor.detect`: detector, then `policy.filter`, then `policy.anchor_spans` unioned in, then validate and merge. The anchor comes after the filter, so it is never filtered. The `PiiDetector` port and `Detection` are unchanged. |
| Anchor matching | Names: whole phrase, plus 3+ char tokens found in an email local part. Emails: literal, case-insensitive. Phones: equal digits, or one a suffix of the other with 7+ digits. |
| Identity file | `pii.identity_path` (`profile/identity.yaml`, gitignored; shape in `profile/identity.example.yaml`) pins values derivation cannot see. Absent is silent. Present values are unioned in and count as `confirmed`. Malformed raises at build time. |
| Cost | Third-party single-word names are not redacted. The recall pass does not change this. |
| Disclosure | `/api/resume` carries `privacy`: enabled, counts, full `redactedPreview`, `ownerAnchor` (level only), `unavailableReason`. It is a preview of this CV, not a transcript of a request. When detection fails the preview is `null` and scoring calls fail rather than send raw text. |
| Unsupported | Streaming, tool specs, tool results, image or file parts are not expressible in `complete()` and must be rejected until a scanning path exists. |

## Shapes

```python
# ports.py
@dataclass(frozen=True, slots=True)
class Detection:  # one span, offsets into the string as given
    start: int; length: int; category: str  # PERSON|EMAIL|PHONE|ADDRESS|URL|IBAN|CREDIT_CARD|IP
class PiiDetector(Protocol):  # one span list per input text
    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]: ...
class PiiRedactionError(LLMCallError): ...   # provider never called
class PiiModelMissingError(PiiRedactionError): ...  # names the missing model

# domain/owner_identity.py
@dataclass(frozen=True, slots=True)
class OwnerIdentity:  # derived, never persisted
    names: tuple[str, ...]; emails: tuple[str, ...]; phones: tuple[str, ...]
    confidence: Literal["confirmed", "likely", "none"]
```

```yaml
pii:                                 # config.py PiiSpec; all values are the defaults
  enabled: true
  entities: [PERSON, EMAIL, PHONE, ADDRESS, URL, IBAN, CREDIT_CARD, IP]
  llm_pass: false                    # opt in; needs Ollama
  llm_endpoint: http://localhost:11434
  llm_allow_remote: false
  language: en
  llm_model: qwen2.5:3b
  llm_timeout_seconds: 30
  llm_failure: block                 # block | presidio_only
  outbound: warn                     # allow | warn | block
  owner_anchor: true
  drop_single_token_person: true
  person_false_positive_terms: []
  identity_path: null                # e.g. profile/identity.yaml
```

The spaCy model is not installed by `uv sync`: run `uv run python -m spacy download en_core_web_lg` once per machine.

## Flow

```
complete(prompt, schema, system, validator):
    if prompt or system is not str:  raise PiiRedactionError
    r = redactor.redact([prompt, system])      # detector error, bad span -> PiiRedactionError
    resp = inner.complete(r.texts..., validator = lambda d: validator(r.restore(d)))
    data = r.restore(resp.data)                # leftover "<|" -> PiiRedactionError
    outbound(data)                             # scanner error: log, return; block: raise
    return data                                # r dies here

Redactor.detect(text):
    spans = detector.detect(text)
    spans = policy.filter(spans, identity)     # drop one-word PERSON, exempt terms, fake PHONE
    spans += policy.anchor_spans(text, identity)
    return validate(spans)

Redactor.learn(cv_text, filename):             # once per run, before any call
    if not owner_anchor: return none
    identity = derive(cv_text, policy.filter(detect(cv_text)), filename) + identity file
```

## Testing

| Layer | What it proves |
|---|---|
| pytest unit `test_redact.py` | Numbering, shared tokens, longest-first restore, right-to-left replace, overlap merges and adjacency does not, bad spans and residue fail closed, an omitted placeholder does not. |
| pytest unit `test_redacting_client.py`, contract `test_redacting_llm_client.py` | The inner client never sees a mapping value (asserted on `FakeLLM` calls). The validator sees restored data. Both retry attempts carry redacted text. A detector failure never reaches the inner client. Outbound modes behave; a scanner failure returns the reply. |
| pytest unit `test_presidio_detector.py`, `test_ollama_detector.py`, `test_hybrid_detector.py` | Category mapping and allowlist; `LOCATION` is not redacted; street pattern fires; non-substring model spans are dropped; union and `llm_failure` modes. |
| pytest unit `test_owner_identity.py`, `test_redactor_person_policy.py` | Email and filename cross-checks win; footer names win as top names do; proximity-only reports `likely`; none reports `none`; derivation is repeatable. `Kafka`, `Grafana` survive, `Ada Lovelace` is redacted, `Kafka Rodrigues` is not exempt; other categories are never suppressed; a body-text first name and phone variants are caught; the Kafka/Grafana excerpt round-trips. |
| pytest unit `test_pii_factory.py` | Defaults on an omitted block; remote endpoint rejected; absent identity file silent, malformed raises; the seed includes `core_skills`; `owner_anchor: false` disables derivation. |
| pytest contract `test_resume_api.py` | The preview has no technical term replaced, no owner name, and `ownerAnchor` is a level, never a value. |
| pytest, caplog | No mapping or identity value in logs after success or failure. |
| Manual | `scripts/verify_pii_boundary.py "<name>" <email>` captures the provider payload in process and prints PASS or FAIL only. Run it with `ollama stop` and `llm_failure: block` to see a non-zero abort. Upload the real CV and check the preview. |

CI runs all pytest rows (no network). Presidio rows use `en_core_web_sm` and skip cleanly when it is absent. The script and the real-CV check are manual. Correctness is judged on what reaches the inner client, not on the redactor's own output.

## Non-goals

An HTTP proxy, redacting employers or dates, separate policy for public posting text, and fake-data substitution.
