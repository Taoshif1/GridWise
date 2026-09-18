# GridWise Final Submission Checklist

This checklist follows the BUP CSE Fest 2026 Participant Guide & Evaluation Rubric.

## API and schema

- [x] Public base URL exists: https://gridwise-bup.onrender.com
- [x] `GET /health` is implemented.
- [x] `POST /optimize-energy` is implemented.
- [x] Request validation requires exactly 24 unique hours `0..23`.
- [x] Request validation requires 1–3 non-empty `operator_notes`.
- [x] Response includes `scenario_id`, `directive_interpretation`, `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, and `plan_summary`.

## LLM interpretation

- [x] Google Gemini is in the actual `operator_notes` interpretation path.
- [x] Exactly one interpretation entry is expected per note.
- [x] Entries remain in `note_index` order.
- [x] `no_op` uses `applies=false` and `structured_adjustment=null`.
- [x] Non-`no_op` directives use `applies=true`.
- [x] Paraphrased time windows and solar-reduction factors are normalized by the language model prompt.
- [x] Multiple Gemini keys can be configured for provider fallback.

## Deterministic guardrails

- [x] Only organizer-supported directive types are accepted.
- [x] Hours must be unique ascending integers `0..23`.
- [x] Solar factor is constrained to `0..1`.
- [x] Battery reserve values are finite, non-negative, and bounded by capacity.
- [x] Grid caps are finite and non-negative.
- [x] Unsupported / malformed model output fails safely.
- [x] The LLM cannot directly change base demand, tariff, or battery parameters.

## Energy and optimization

- [x] Solar reductions are applied before optimization.
- [x] Minimum reserve directives are enforced.
- [x] No-charge directives are enforced.
- [x] No-discharge directives are enforced.
- [x] Max-grid directives are enforced.
- [x] Hourly energy balance is checked.
- [x] Battery capacity, minimum, transition, and hourly rate limits are checked.
- [x] Solar use cannot exceed effective solar.
- [x] Final battery energy equals initial battery energy.
- [x] Objective minimizes grid cost after validity.

## Final replay

- [x] All 24 hours are independently replayed.
- [x] `total_grid_kwh` is independently recalculated.
- [x] `total_cost_bdt` is independently recalculated.
- [x] `peak_grid_kwh` is independently recalculated.

## Reliability and security

- [x] Malformed input returns controlled failure.
- [x] Bad LLM output does not silently create a directive.
- [x] Provider failures are handled without exposing secrets.
- [x] API keys are runtime environment variables.
- [x] `.env` is excluded from Git and Docker images.
- [ ] Re-test repeated live requests immediately before submission.
- [ ] Confirm p95 latency is acceptable on the final hosted environment.
- [ ] Confirm every valid request remains below the organizer 30-second timeout.

## Deployment

- [x] Render live service is deployed.
- [x] Service binds to `0.0.0.0`.
- [x] Public URL requires no login, VPN, or manual approval.
- [ ] Confirm hosted Gemini keys/quota are valid through the judging window.

## Docker fallback

- [x] Dockerfile exists in the restored source.
- [x] GitHub Actions GHCR publishing workflow is configured.
- [ ] Confirm `ghcr.io/taoshif1/gridwise:latest` was successfully published.
- [ ] Make the GHCR package pullable by judges.
- [ ] Verify documented `docker pull` and `docker run` commands.
- [ ] Verify container `/health` from a clean machine.
- [ ] Record an exact tag or digest in the final submission form if required.

## Documentation and reproducibility

- [x] README documents the architecture.
- [x] README documents model/provider and LLM role.
- [x] README documents deterministic guardrails.
- [x] README documents optimizer/solver.
- [x] README lists environment-variable names.
- [x] README includes exact local run commands.
- [x] README includes health and optimize curl examples.
- [x] README lists dependencies and known limitations.
- [x] README documents secret handling.
- [ ] Add and execute the organizer Public Sample Cases JSON if/when supplied.

## Repository policy

- [x] Repository exists for the round solution.
- [x] Repository currently remains private during the event.
- [ ] Make the repository public after the submission deadline, following organizer instructions.

## 3-minute video

- [x] A technical video script is included.
- [ ] Record/export the final video at 3:00 or less.
- [ ] Upload the MP4 or organizer-accessible link.
- [ ] Add the final video link to README and submission form.
- [ ] Verify judges can access the link without requesting permission.
