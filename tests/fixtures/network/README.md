# Historical root build-context policy

`root-93f21856.dockerignore` is a byte-for-byte copy of `.dockerignore` at
`93f21856c2d824e68b7d9f5c97b5d8ff9dc5f196` in this repository.
SHA-256: `b08f17c51e2059441ae086bf9d4e16dbec4a4ec3b41f3377c26b96a2dc2fcad5`.

The native smoke uses this inert fixture for its historical exposure control.
It runs from a synthetic source archive without Git history, so the regression
remains runnable after squash merges and in shallow checkouts. The historical
policy must expose the synthetic IDNA sibling; current legacy and BuildKit
policies must exclude it. This file never configures a production build.
