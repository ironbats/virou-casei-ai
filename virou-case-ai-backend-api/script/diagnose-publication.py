import argparse
import json

from viracase.infrastructure.publication_build import IsolatedBuild


def main():
    parser = argparse.ArgumentParser(description="Diagnostico local do executor; nao chama a Cloudflare.")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    diagnostics = IsolatedBuild().diagnostics()
    print(json.dumps(diagnostics, ensure_ascii=False, sort_keys=True, indent=2 if args.pretty else None))
    return 0 if diagnostics["available"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
