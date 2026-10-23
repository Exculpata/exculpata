# Gateway nginx.conf fixture

`nginx.conf` is the unmodified `/etc/nginx/nginx.conf` from the Linux amd64
image pinned by `deploy/gateway/Dockerfile`:

- Image index: `sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94`
- Platform manifest: `sha256:8f84ed99befc3891b8f329c5c202785278a2cfb7c25107d57fb2a134a3117433`
- Configuration SHA-256: `4dee6b0c7c8278f6a9b827261c25d3215c9ea8955e41ff7fe3eabdd5297edce7`

The test pins the entire enclosing configuration so changes to log formats,
destinations, levels, scopes or includes require review. The real gateway
contract checks the running container's file against the same pin; the fixture
alone is not deployed-configuration evidence. Mutation cases check that changed
logging directives are rejected without requiring Docker for those cases.

When updating the image, review its enclosing configuration together with the
repository gateway template, then update this fixture and the pin only if the
content-free logging contract still holds. Run the real gateway contract too.
