# SWE image mirror canaries

`openreward-swe-image-mirror-canary.json` is an explicit, opt-in image map for
the Kubernetes SWE runtime. It does not alter the official SWE-bench harness
image identity or task manifest; it only changes the Pod image reference after
the harness has computed the canonical reference.

The first entry is a compatibility canary for
`astropy__astropy-12907`. The internal image was checked in Kubernetes and its
`/testbed` `HEAD` is `d16bfe05a744909de4b27f5875fe0d4ed41ce607`, matching the
pinned SWE-bench manifest. It is not yet evidence that all verifier behavior
matches the canonical image. Canonical images for the eight production canary
tasks should replace these entries before claiming official SWE-bench parity.

The second entry is `scikit-learn__scikit-learn-14141`. Its Kubernetes
`/testbed` `HEAD` is `3d997697fdd166eff428ea9fd35734b6a8ba113e`, also matching
the pinned manifest. The issue and gold change are deliberately small, making
it suitable for a policy-generated positive-reward canary.
