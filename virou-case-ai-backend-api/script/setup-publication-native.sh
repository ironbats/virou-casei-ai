#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
for executable in npm bwrap; do
  command -v "$executable" >/dev/null || { echo "Instale Node.js, npm e bubblewrap do sistema antes de continuar." >&2; exit 1; }
done
[[ -x /usr/bin/node && -x .venv/bin/python ]] || { echo "É necessário /usr/bin/node e o ambiente .venv do backend." >&2; exit 1; }
npm --prefix build-toolchain ci --ignore-scripts --no-audit --no-fund
/usr/bin/node build-toolchain/node_modules/esbuild/install.js
.venv/bin/python -m pip install --disable-pip-version-check workers-py==1.17.9 uv==0.13.4
.venv/bin/python build-toolchain/vendor-python.py
.venv/bin/python -c 'from viracase.infrastructure.publication_build import IsolatedBuild; builder = IsolatedBuild(); print(builder.diagnostics()); builder.verify_isolation()'
echo "Executor preparado e isolamento verificado. Reinicie o worker e faça a verificação da conexão no backoffice."
