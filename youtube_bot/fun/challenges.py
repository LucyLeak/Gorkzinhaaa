from __future__ import annotations

import random


def mirror_mode(text: str) -> str:
    words = text.split()
    mirrored = " ".join(reversed(words))
    return f"Espelho ativado: {mirrored}"


def quick_rhyme(word: str) -> str:
    clean = word.strip(" .,!?:;").lower()
    if not clean:
        return "Me da uma palavra e eu tento rimar."
    endings = ["ao", "inha", "eiro", "ado", "ente"]
    ending = next((item for item in endings if clean.endswith(item)), clean[-2:])
    options = [
        f"{clean} entrou no chat com estilo e direcao.",
        f"Se a palavra e {clean}, minha rima vem ligeiro.",
        f"{clean} na tela, resposta na mente.",
    ]
    if ending == "ao":
        options.append(f"{clean} virou verso de improvisacao.")
    return random.choice(options)


def emoji_reaction(text: str) -> str | None:
    if "🔥" in text:
        return "🔥 Energia detectada. Vou fingir que foi tudo calculado."
    if "😂" in text or "🤣" in text:
        return "😂 Risada registrada. Minha autoestima subiu 3 pixels."
    if "❤️" in text or "<3" in text.lower():
        return "Valeu pelo carinho. O algoritmo quase sorriu."
    return None
