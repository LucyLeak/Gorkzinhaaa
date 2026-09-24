"""Testa se o endpoint de compatibilidade da Cohere honra response_format json_object.

Uso (a partir da raiz do repositorio):
    python tools/test_cohere_json_mode.py --api-key SUA_CHAVE_COHERE
ou, com as variaveis OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_CHAT_MODEL no .env:
    python tools/test_cohere_json_mode.py

Faz duas chamadas para {base_url}/chat/completions:
  1. com    response_format={"type": "json_object"}
  2. sem    response_format (controle)
Imprime o status HTTP e o corpo bruto de cada resposta e um veredito simples.
Usa somente biblioteca padrao (urllib), sem dependencias novas.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.cohere.com/compatibility/v1"
DEFAULT_MODEL = "command-r-plus-08-2024"

PROMPT = (
    'Responda SOMENTE com um objeto JSON valido, sem texto fora do JSON, no formato '
    '{"thought":"<raciocinio interno, nunca enviado ao chat>",'
    '"message":"<mensagem final enviada ao chat>"}. '
    "Diga ola para o chat do canal."
)


def chat_completions(base_url: str, api_key: str, model: str, payload: dict) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def extract_content(body: str) -> str:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return ""
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def looks_like_contract(content: str) -> bool:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(data, dict)
        and isinstance(data.get("message"), str)
        and "thought" in data
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument(
        "--base-url",
        default=os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
    )
    parser.add_argument(
        "--model",
        default=os.getenv("OPENAI_CHAT_MODEL") or DEFAULT_MODEL,
    )
    args = parser.parse_args()
    if not args.api_key:
        print("ERRO: informe --api-key ou defina OPENAI_API_KEY no ambiente.", file=sys.stderr)
        return 2

    base_payload = {
        "model": args.model,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 220,
        "temperature": 0.3,
    }

    print(f"Base URL: {args.base_url}")
    print(f"Modelo:   {args.model}\n")

    print("=== 1) COM response_format json_object ===")
    status_json, body_json = chat_completions(
        args.base_url, args.api_key, args.model,
        {**base_payload, "response_format": {"type": "json_object"}},
    )
    print(f"HTTP {status_json}")
    print(body_json)
    print()

    print("=== 2) SEM response_format (controle) ===")
    status_ctrl, body_ctrl = chat_completions(
        args.base_url, args.api_key, args.model, base_payload,
    )
    print(f"HTTP {status_ctrl}")
    print(body_ctrl)
    print()

    json_mode_ok = status_json == 200 and looks_like_contract(extract_content(body_json))
    control_ok = status_ctrl == 200 and looks_like_contract(extract_content(body_ctrl))

    print("=== Veredito ===")
    if json_mode_ok:
        print("response_format json_object: SUPORTADO (resposta veio em JSON valido).")
        print("Voce pode definir OPENAI_JSON_MODE=true no Render.")
    elif status_json == 400:
        print("response_format json_object: REJEITADO (HTTP 400). Mantenha OPENAI_JSON_MODE=false.")
    elif status_json == 401 or status_ctrl == 401:
        print("Autenticacao falhou (HTTP 401); teste inconclusivo. Confira a chave.")
    else:
        print(
            f"Resposta inesperada (HTTP {status_json}); mantenha OPENAI_JSON_MODE=false "
            "e analise o corpo acima."
        )
    print(f"Controle (sem response_format) retornou JSON valido: {'sim' if control_ok else 'nao'}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
