# CAIRNML_FROM chooses where the SDK (cairnml) and the agent (cairnml-agent) come from:
#   pypi    (the default, and the release build) the versions pinned in requirements.lock
#   source  the source trees passed as the named contexts cairnml_sdk and cairnml_agent, so the image runs the
#           working tree; the stack (deploy/, the end-to-end suite) builds this way
ARG CAIRNML_FROM=pypi

FROM node:24-bookworm-slim AS webapp_builder

USER root
WORKDIR /opt

# The web app is our fork of cairnml-web. It is passed as the named build context "cairnml_web"
# (deploy/compose.yaml does this), so the image contains the fork's changes and not an
# upstream tag. Build with: --build-context cairnml_web=<path to the cairnml-web fork>
COPY --from=cairnml_web . /opt/open-webapp
COPY --chmod=744 docker/build/internal_files/build_webapp.sh /tmp/internal_files/
RUN /bin/bash -c '/tmp/internal_files/build_webapp.sh'

FROM scratch AS sdk-pypi
FROM scratch AS agent-pypi
FROM cairnml_sdk AS sdk-source
FROM cairnml_agent AS agent-source
FROM sdk-${CAIRNML_FROM} AS sdk
FROM agent-${CAIRNML_FROM} AS agent

FROM python:3.11-slim-bookworm
ARG CAIRNML_FROM
COPY --chmod=755 docker/build/internal_files/entrypoint.sh /opt/cairnml/
COPY --chmod=755 docker/build/internal_files/update_from_env.py /opt/cairnml/utilities/
COPY apiserver /opt/cairnml/apiserver/

COPY --chmod=744 docker/build/internal_files/final_image_preparation.sh /tmp/internal_files/
COPY docker/build/internal_files/cairnml.conf.template /tmp/internal_files/
# The source trees are mounted, not copied, so they never become a layer of the image.
RUN --mount=type=bind,from=sdk,source=/,target=/tmp/cairnml-sdk,rw \
    --mount=type=bind,from=agent,source=/,target=/tmp/cairnml-agent,rw \
    /bin/bash -c '/tmp/internal_files/final_image_preparation.sh'

# The Angular application builder writes the app into build/browser; that folder replaces nginx's default page.
# The entrypoint writes runtime configuration into the web root, so the server user owns it.
COPY --chown=cairnml:cairnml --from=webapp_builder /opt/open-webapp/build/browser /usr/share/nginx/html
COPY --from=webapp_builder /opt/open-webapp/dist/report-widgets /usr/share/nginx/widgets

EXPOSE 8080
EXPOSE 8008

ARG VERSION
ARG BUILD
ENV CAIRNML_SERVER_VERSION=${VERSION}
ENV CAIRNML_SERVER_BUILD=${BUILD}

WORKDIR /opt/cairnml/
# The processes run as uid 1000, which is the owner of the folders they write to (see final_image_preparation.sh).
USER cairnml
ENTRYPOINT ["/opt/cairnml/entrypoint.sh"]
