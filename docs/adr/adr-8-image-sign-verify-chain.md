# ADR-8: Image sign/verify chain — attest v0.2, verify in CI, verify at deploy

## Status

Accepted.

## Context

The release pipeline cosign-signs and attests every image under a keyless
GitHub-OIDC identity (certificate-identity/oidc-issuer pinned to the
workflow), and the digests are pinned into `deploy/`. Examined end to end,
the chain turned out broken at three links:

1. **Attestation shape.** release.yml generated the SLSA predicate inline
   with the *v1* shape (`predicate.buildDefinition`,
   `predicate.runDetails.builder`), but `cosign attest --type
   slsaprovenance` validates the *v0.2* schema, where `builder` is a
   top-level predicate field. Every attest since the inline generator
   landed failed `required field builder missing` — the `:dev` image was
   never signed at all.
2. **CI verification.** The ci.yml cosign-verify job extracted the `:dev`
   digest via `imagetools inspect --raw | jq .digest`; the raw manifest has
   no top-level `.digest`, so the extraction was null by design and the job
   had never passed — two bugs masking each other, since with signing
   broken there was nothing to verify anyway.
3. **Deploy-time verification.** Even with CI green, nothing verified the
   signature where it matters — when the kubelet pulls the image. Digest
   pinning protects against tag drift, not against a compromised registry
   account pushing a new manifest that a future manifest edit references.

## Decision

- The predicate is the bare v0.2 `ProvenancePredicate`; cosign's `--type
  slsaprovenance` unmarshals it into the v0.2 struct and constructs the
  Statement envelope itself. `slsaprovenance` (v0.2) is the shared contract
  of all four verifiers (release dev-verify, release tag-verify, CI
  `:dev`-verify, CI tag-verify) — no custom predicate type.
- CI verification reads the `:dev` digest from the registry descriptor (not
  the raw manifest) and proves the image carries a provenance attestation
  from the pinned workflow identity.
- **Ordering and independence (issue #588).** Verification is now strictly
  ordered and never races the signer: the release's digest-pin commit is
  the *last* step of `publish-dev` — after `Sign + attest` and an
  in-workflow verify of the exact `@sha256:` digest just signed (not the
  mutable `:dev` tag) — so a failed sign can never leave develop pinned to
  an unsigned digest; ci.yml's `:dev` verifier is `workflow_run`-gated on
  the Release workflow's *successful* completion, so it deterministically
  verifies the digest that run just signed instead of racing it; and `v*`
  tag pushes get an independent `cosign-verify-tag-image` job in ci.yml
  (separate workflow from the signer, #159 pattern, #169 tag identity,
  bounded retry while the Release run signs concurrently), closing the tag
  blind spot that previously had only release.yml's self-check.
- Deploy-time verification:
  `scripts/deploy-openstudio-stack.sh` extracts the digest-pinned image
  from `deploy/` and runs `cosign verify` with the exact
  certificate-identity/oidc-issuer pair from ci.yml — a **loud skip** when
  cosign is absent (kind dev must not hard-require it) and an **abort** on
  verification failure. `docs/validation.md` documents the copy-paste
  command plus a Kyverno `verifyImages` Enforce sketch, so production
  clusters have an enforcement path.

## Consequences

- The v0.2 shape is load-bearing: changing the generator's predicate shape
  or any verifier's `--type` breaks all three verifiers at once — which is
  also what keeps the shared contract easy to align.
- The certificate-identity/oidc-issuer pair is stated in three places
  (ci.yml, the deploy script, the validation docs) and must move together;
  the identity is only as strong as the workflow's SHA-pinned actions.
- Deploy-time verification is skip-loud rather than hard-required: clusters
  that want enforcement adopt the Kyverno sketch; the script's
  abort-on-failure keeps kind deployments honest whenever cosign is present.
- Attestation proves who built the image, not that the toolchain was clean
  — the pinned build backend (ADR-6) closes that adjacent gap, and the
  scan gates (ADR-7) cover the *content* dimension the signature does not.

## Links

- Issues #456 (v0.2 predicate shape) · #459 (CI digest extraction) · #500
  (deploy-time verify) · #155 (the original inline-predicate generator) ·
  #169 (certificate-identity pins) · #149 / #291 (digest-pin commit) ·
  #389 (SHA-pinned actions) · #588 (ordering + workflow_run gate + tag
  verification)
- `.github/workflows/release.yml` · `.github/workflows/ci.yml`
  (`cosign-verify-dev-image`, `cosign-verify-tag-image`)
- `scripts/deploy-openstudio-stack.sh` · `docs/validation.md`
- ADR-6 (build-toolchain pins) · ADR-7 (pre-pin content gates)
