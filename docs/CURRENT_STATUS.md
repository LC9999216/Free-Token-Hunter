# Free Token Hunter current status

Updated: 2026-09-17 (Asia/Shanghai)

This file is the current status source. `PROGRESS.md` records the completed Stage 1 task history;
the older Stage 2 progress files are audit logs and may include historical stop reports.

## Code state

- Release-readiness branch: `codex/stage2-release-ready`.
- Baseline: `codex/stage2-live-validation-remediation@d46e222`.
- Stage 1 tasks 000–010: implemented.
- Stage 2 offline hardening and remediation: implemented locally, not merged to `main`.
- Production grounded extraction: wired to an OpenAI-compatible Chat Completions endpoint through
  `HUNTER_LLM_BASE_URL`, `HUNTER_LLM_API_KEY`, and `HUNTER_LLM_MODEL`.
- Release runs can require that boundary with `run-stage-one --require-llm`; missing or partial
  configuration exits with code 2.
- The LLM transport sends the key only in the Authorization header, ignores system proxies, rejects
  redirects, bounds response size, and does not retain upstream response bodies.

## Fresh offline verification

- Full suite: `728 passed, 1 skipped, 1 warning in 114.96s`.
- Skip: POSIX permission-bit assertion on Windows.
- Warning: `getpass` fallback in a non-interactive test terminal.
- Focused production-LLM tests: `11 passed`.

## Protected persisted data

The repository data remains unchanged:

- Candidates: 69.
- Evidence: 0.
- Providers: 2 `UNCERTAIN`.
- `FREE_CONFIRMED`: 0.

No real key, provider request, webhook delivery, promotion, deployment, push, PR, or merge was
performed while producing this status.

## Remaining live gates

1. Run evidence collection from a host whose DNS returns public provider addresses. The current
   Codex host resolves `developers.cloudflare.com` to reserved address `198.18.0.104`, so
   `SafeFetcher` correctly refuses the connection. Do not weaken SSRF validation to bypass this.
2. Configure a real JSON-capable OpenAI-compatible extraction model through process environment
   variables and run Stage 1 against an external data copy with `--require-llm`.
3. Stop unless Stage 1 legitimately produces at least one anchored, grounded `FREE_CONFIRMED`
   provider. Never edit Registry status or Evidence by hand.
4. Obtain explicit authorization for that provider's API key, enter it through Pool Control
   `getpass`, and then execute staging health plus chat, responses, streaming, and tools canaries.
5. Complete approval binding, promotion readback, Codex/OpenCode/client interoperability,
   downgrade/suspension, and Feishu delivery gates in order.

The project is offline release-ready, but it is not live-validated or production-ready until these
external gates pass.
