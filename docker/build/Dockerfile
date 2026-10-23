FROM node:24-bookworm-slim as webapp_builder

USER root
WORKDIR /opt

# The web app is our fork of cairnml-web. It is passed as the named build context "cairnml_web"
# (deploy/cairnml/compose.yaml does this), so the image contains the fork's changes and not an
# upstream tag. Build with: --build-context cairnml_web=<path to the cairnml-web fork>
COPY --from=cairnml_web . /opt/open-webapp
COPY --chmod=744 docker/build/internal_files/build_webapp.sh /tmp/internal_files/
RUN /bin/bash -c '/tmp/internal_files/build_webapp.sh'

FROM python:3.11-slim-bookworm
COPY --chmod=744 docker/build/internal_files/entrypoint.sh /opt/cairnml/
COPY --chmod=744 docker/build/internal_files/update_from_env.py /opt/cairnml/utilities/
COPY apiserver /opt/cairnml/apiserver/

COPY --chmod=744 docker/build/internal_files/final_image_preparation.sh /tmp/internal_files/
COPY docker/build/internal_files/cairnml.conf.template /tmp/internal_files/
COPY docker/build/internal_files/cairnml_subpath.conf.template /tmp/internal_files/
RUN /bin/bash -c '/tmp/internal_files/final_image_preparation.sh'

# The Angular application builder writes the app into build/browser; that folder replaces nginx's default page.
COPY --from=webapp_builder /opt/open-webapp/build/browser /usr/share/nginx/html
COPY --from=webapp_builder /opt/open-webapp/dist/report-widgets /usr/share/nginx/widgets

EXPOSE 8080
EXPOSE 8008

ARG VERSION
ARG BUILD
ENV CAIRNML_SERVER_VERSION=${VERSION}
ENV CAIRNML_SERVER_BUILD=${BUILD}

WORKDIR /opt/cairnml/
ENTRYPOINT ["/opt/cairnml/entrypoint.sh"]
