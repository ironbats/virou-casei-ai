import argparse
import http.cookiejar
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]


def command(*args):
    result = subprocess.run(args, check=False, cwd=BACKEND, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            "Falha no comando " + " ".join(args[:3]) + ". Verifique Docker, Compose e os serviços."
        )
    return result.stdout.strip()


def inspect_service(service, project=None, completed=False):
    if project:
        ids = command(
            "docker",
            "ps",
            "-aq",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        )
    else:
        ids = command("docker", "compose", "ps", "--all", "--quiet", service)
    if not ids:
        raise RuntimeError(f"Container ausente: {service}. Inicie os três projetos primeiro.")
    for container in json.loads(command("docker", "inspect", *ids.split())):
        state = container["State"]
        if completed:
            valid = state["Status"] == "exited" and state["ExitCode"] == 0
        else:
            valid = state["Status"] == "running" and state.get("Health", {}).get("Status") == "healthy"
        if not valid:
            raise RuntimeError(f"Container {service} ainda não passou na verificação de saúde/inicialização.")
    print(f"✅ Container {service}: {'inicialização concluída' if completed else 'running e healthy'}.")


def http_request(opener, url, expected, *, body=None, origin=None, csrf=None):
    headers = {}
    if origin:
        headers["Origin"] = origin
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode(), headers=headers
    )
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        content = response.read()
        if response.status != expected:
            raise RuntimeError(
                f"HTTP {response.status} em {url}; esperado {expected}. Nenhuma credencial foi exibida."
            )
        return content


def main():
    parser = argparse.ArgumentParser(
        description="Verifica containers, proxy e origens sem alterar contas ou projetos."
    )
    parser.add_argument(
        "--check-admin-login",
        action="store_true",
        help="Testa login/logout com as credenciais administrativas do .env.",
    )
    args = parser.parse_args()
    for service in ("postgres", "public-api", "admin-api", "worker"):
        inspect_service(service)
    for service in ("storage-init", "initialize"):
        inspect_service(service, completed=True)
    inspect_service("frontend", "viroucaseai-frontend")
    inspect_service("backoffice", "viroucaseai-backoffice")
    credentials = None
    if args.check_admin_login:
        config = json.loads(command("docker", "compose", "config", "--format", "json"))
        environment = config["services"]["initialize"]["environment"]
        login = environment.get("VIRA_ADMIN_LOGIN", "").strip().lower()
        password = environment.get("VIRA_ADMIN_PASSWORD", "")
        if not login or not password:
            raise RuntimeError(
                "Preencha VIRA_ADMIN_LOGIN e VIRA_ADMIN_PASSWORD para testar o login. Administradores existentes não são redefinidos."
            )
        credentials = {"login": login, "password": password}
    for host in ("localhost", "127.0.0.1"):
        for port, prefix, admin in ((5173, "/api/v1", False), (5174, "/admin-api/v1", True)):
            origin = f"http://{host}:{port}"
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            )
            html = http_request(opener, origin + "/", 200).decode()
            if "viroucaseai" not in html.lower():
                raise RuntimeError(f"Marca esperada ausente em {origin}. Reconstrua a imagem do frontend.")
            ready = json.loads(http_request(opener, origin + "/health/ready", 200))
            if ready.get("status") != "ready":
                raise RuntimeError(f"Proxy de prontidão inválido em {origin}.")
            path = origin + prefix
            fake = {"login": "check-" + uuid4().hex, "password": "invalid-verification-password"}
            http_request(opener, path + "/auth/login", 403, body=fake, origin="https://untrusted.invalid")
            http_request(opener, path + "/auth/login", 401, body=fake, origin=origin)
            http_request(opener, path + "/me", 401)
            print(f"✅ {origin}: marca, proxy, prontidão e restrição de origem.")
            if admin and credentials:
                data = json.loads(
                    http_request(opener, path + "/auth/login", 200, body=credentials, origin=origin)
                )
                http_request(opener, path + "/me", 200)
                http_request(opener, path + "/auth/logout", 403, body={}, origin=origin)
                http_request(
                    opener, path + "/auth/logout", 204, body={}, origin=origin, csrf=data["csrf_token"]
                )
                http_request(opener, path + "/me", 401)
                print(f"✅ {origin}: login administrativo, cookie de sessão, CSRF e logout.")
    if not credentials:
        print(
            "Credenciais administrativas não testadas. Use --check-admin-login para validar o login informado no .env."
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        print(f"❌ Verificação interrompida: {error}", file=sys.stderr)
        raise SystemExit(1) from None
