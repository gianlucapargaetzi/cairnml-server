#!/usr/bin/env bash
set -x
set -o errexit
set -o nounset
set -o pipefail

apt-get update -y
apt-get install -y python3-setuptools python3-dev build-essential nginx gettext vim curl git

python3 -m ensurepip
python3 -m pip install --upgrade pip
# The lock has exact versions and hashes, so two builds of the same commit install the same packages.
python3 -m pip install --require-hashes -r /opt/cairnml/apiserver/requirements.lock
if [ "${CAIRNML_FROM:-pypi}" = source ]; then
    # The SDK is the source tree of this build, not the released version of the lock; its agent extra comes with it.
    # Build output left in a checkout is not source, so it is removed before the install. The install takes the
    # dependencies too (the lock is installed above, so only the missing packages come from the index).
    tree=/tmp/cairnml-sdk
    rm -rf "$tree/build" "$tree/dist" "$tree"/*.egg-info
    python3 -m pip install "${tree}[agent]"
    # pip check also reports unrelated packages of the base image (the wheel package needs a newer packaging than the
    # lock); only a broken dependency of the SDK fails the build.
    if python3 -m pip check | grep -qi cairnml; then
        python3 -m pip check || true
        echo "The SDK has an unmet dependency" >&2
        exit 1
    fi
fi
mkdir -p /opt/cairnml/log
mkdir -p /opt/cairnml/config
ln -svf /dev/stdout /var/log/nginx/access.log
ln -svf /dev/stderr /var/log/nginx/error.log
mv /tmp/internal_files/cairnml.conf.template /etc/nginx/cairnml.conf.template

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
# configuration into sites-enabled and default.d, the web root (see the COPY in the Dockerfile), and the link that
# serves the web root under its path (cairnml-root).
sed -i -e 's|^pid .*|pid /tmp/nginx.pid;|' -e '/^user /d' /etc/nginx/nginx.conf
rm -f /etc/nginx/sites-enabled/default
mkdir -p /etc/nginx/sites-enabled /etc/nginx/default.d /var/cache/nginx /var/lib/nginx /usr/share/nginx/html /usr/share/nginx/cairnml-root
chown -R cairnml:cairnml /etc/nginx/sites-enabled /etc/nginx/default.d /var/cache/nginx /var/lib/nginx /usr/share/nginx/html /usr/share/nginx/cairnml-root

rm -d -r "$(pip cache dir)"
apt-get clean
