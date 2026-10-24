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

rm -d -r "$(pip cache dir)"
apt-get clean
