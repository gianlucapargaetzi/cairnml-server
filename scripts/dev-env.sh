#!/usr/bin/env bash
# Creates the virtual environment .venv at the root of this repository. The SDK and the agent are installed editable from
# their submodules, and the server's other requirements from apiserver/requirements.txt, so the tests and
# the scripts run the working tree and not the last release. Run it again after pulling or switching branches.
#
# All of it is one pip resolution, so a conflict between the packages is reported here and not left behind as a warning.
# The SDK's dataset extra is dvc and pyarrow. Only pyarrow is here: dvc needs dpath 2, and the server pins dpath below 2.
# The SDK's tests pass without dvc.
set -euo pipefail
root=$(git rev-parse --show-toplevel)
cd "$root"

python3 -m venv --clear .venv
.venv/bin/python -m pip install --quiet --upgrade pip
# The two packages are the submodules; their lines are left out of the requirements so pip never fetches them from PyPI.
# editable_mode=compat puts the source folders on sys.path. The default finder loses to the cairnml/ submodule folder at
# the root of this repository, so from the root "import cairnml" would find that folder and not the package.
.venv/bin/python -m pip install --quiet \
  --config-settings editable_mode=compat \
  -e cairnml -e cairnml-agent pyarrow pytest \
  -r <(grep -vE '^cairnml(-agent)?([<>=!~[:space:]]|$)' apiserver/requirements.txt)

# Every import must point into this working tree, from the root and from anywhere else.
while read -r file; do
  case "$file" in
    "$root"/*) ;;
    *) echo "$file is not in this working tree" >&2; exit 1 ;;
  esac
done < <(cd / && "$root/.venv/bin/python" -c 'import cairnml, cairnml_agent; print(cairnml.__file__); print(cairnml_agent.__file__)')
echo "Ready. Activate with: source .venv/bin/activate"
