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
mkdir -p /opt/cairnml/log
mkdir -p /opt/cairnml/config
ln -svf /dev/stdout /var/log/nginx/access.log
ln -svf /dev/stderr /var/log/nginx/error.log
mv /tmp/internal_files/cairnml.conf.template /etc/nginx/cairnml.conf.template
mv /tmp/internal_files/cairnml_subpath.conf.template /etc/nginx/cairnml_subpath.conf.template

rm -d -r "$(pip cache dir)"
apt-get clean
