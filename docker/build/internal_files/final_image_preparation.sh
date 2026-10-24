#!/usr/bin/env bash
set -x
set -o errexit
set -o nounset
set -o pipefail

apt-get update -y
apt-get install -y python3-setuptools python3-dev build-essential nginx gettext vim curl

python3 -m ensurepip
python3 -m pip install --upgrade pip
# The lock has exact versions and hashes, so two builds of the same commit install the same packages.
python3 -m pip install --require-hashes -r /opt/cairnml/apiserver/requirements.lock
if [ "${CAIRNML_FROM:-pypi}" = source ]; then
    # The SDK and the agent are the source trees of this build, not the released versions of the lock. Build output
    # left in a checkout is not source, so it is removed before the install.
    for tree in /tmp/cairnml-sdk /tmp/cairnml-agent; do
        rm -rf "$tree/build" "$tree/dist" "$tree"/*.egg-info
    done
    python3 -m pip install --no-deps --force-reinstall /tmp/cairnml-sdk /tmp/cairnml-agent
    # pip check also reports unrelated packages of the base image (the wheel package needs a newer packaging than the
    # lock); only a broken dependency of the SDK or the agent fails the build.
    if python3 -m pip check | grep -qi cairnml; then
        python3 -m pip check || true
        echo "The SDK or the agent has an unmet dependency" >&2
        exit 1
    fi
fi
mkdir -p /opt/cairnml/log
mkdir -p /opt/cairnml/config
ln -svf /dev/stdout /var/log/nginx/access.log
ln -svf /dev/stderr /var/log/nginx/error.log
mv /tmp/internal_files/cairnml.conf.template /etc/nginx/cairnml.conf.template
mv /tmp/internal_files/cairnml_subpath.conf.template /etc/nginx/cairnml_subpath.conf.template

# A user of its own, with no login. The code stays owned by root; only the folders below are the server's.
groupadd --gid 1000 cairnml
useradd --uid 1000 --gid cairnml --no-create-home --shell /usr/sbin/nologin cairnml

# The error classes are generated when apiserver.apierrors is imported, and the generator writes its lock into that folder,
# so the server user owns it. The schema cache is written beside the schema on the first start (its import needs Redis,
# so it cannot run here), so that folder is the server user's too.
(cd /opt/cairnml && python3 -m apiserver.apierrors_generator)
mkdir -p /var/log/cairnml
chown -R cairnml:cairnml /opt/cairnml/log /opt/cairnml/config /var/log/cairnml /opt/cairnml/apiserver/apierrors/errors /opt/cairnml/apiserver/schema/services
# nginx keeps its pid file out of /run, which the server user cannot write; the entrypoint writes the site
# configuration into sites-enabled and default.d, and the web root (see the COPY in the Dockerfile).
sed -i -e 's|^pid .*|pid /tmp/nginx.pid;|' -e '/^user /d' /etc/nginx/nginx.conf
rm -f /etc/nginx/sites-enabled/default
mkdir -p /etc/nginx/sites-enabled /etc/nginx/default.d /var/cache/nginx /var/lib/nginx /usr/share/nginx/html
chown -R cairnml:cairnml /etc/nginx/sites-enabled /etc/nginx/default.d /var/cache/nginx /var/lib/nginx /usr/share/nginx/html

rm -d -r "$(pip cache dir)"
apt-get clean
