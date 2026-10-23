# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM nginx:1.30.5-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
# Keep the upstream service contract while applying available distribution fixes.
RUN apk upgrade --no-cache && rm -f /etc/nginx/conf.d/default.conf
ENV NGINX_ENVSUBST_FILTER=^EXCULPATA_
