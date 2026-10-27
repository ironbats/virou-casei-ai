#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v node >/dev/null || { echo "Instale Node.js 22.12+ ou 24 LTS."; exit 1; }
command -v npm >/dev/null || { echo "Instale npm."; exit 1; }
node -e 'const [major,minor]=process.versions.node.split(".").map(Number); if(major<22||(major===22&&minor<12)) process.exit(1)' || { echo "Node.js 22.12+ é necessário."; exit 1; }
npm ci
npm run check
echo "✅ Dependências e tipos verificados. backoffice: http://127.0.0.1:5174"
echo "A API deve estar disponível em 127.0.0.1:8001; execute o script do backend em outro terminal."
exec npm run dev
