# Synthetic assumptions (planted relationships)

These are design choices for synthetic data. They are NOT findings about
real users of any real product.

## Encoded in Phase 1

1. Social degree -> activity. Users with more follows have a higher daily
   active probability, all else equal. Mechanism: degree term in the
   user-day activity probability.
2. Habit -> activity. Being active yesterday raises today's active
   probability. Mechanism: yesterday-active bonus in the user-day loop.
3. Latent engagement -> activity and creation. A per-user latent score
   raises both session frequency and creator probability.
4. Creator tier -> exposure and persistence. Creators are assigned a tier
   (top / mid / long-tail). Impressions are sampled with tier weights, so
   top-tier creators get disproportionate views (concentration is visible
   in the data). Long-tail creators publish less often and are more likely
   to go quiet in the second half of the window.
5. Messaging reciprocity. A message has a substantial probability of a
   reply the same day, so two-way conversations are common and measurable,
   rather than every message being a broadcast.
6. Surface mix differs by app. Instagram-like activity skews to Reels and
   Feed, WhatsApp-like activity skews to messaging, Facebook-like activity
   skews to Feed. This is a modelling choice, not a market claim.

## Deliberately NOT encoded in Phase 1

- Ad load -> satisfaction, crash rate -> retention, notification fatigue,
  toxicity -> blocking, and first-post feedback -> creator retention as a
  causal loop. Those need the Phase 3 tables (ads, reliability,
  notifications, reports) or a two-phase feedback generator. The catalog
  must not claim them from Phase 1 data.
- Any real-world prevalence, revenue, or growth rate. Country shares,
  platform shares, and acquisition mix are illustrative only.

## Noise and confounding

- Day-of-week effects and a mild downward trend in the second half of the
  window are added, so a naive trend read is not automatically a product
  effect.
- Acquisition source correlates with latent engagement (paid users start
  slightly lower on average), so retention by acquisition source is
  confounded in the synthetic data on purpose. The benchmark questions
  must carry that caveat.
