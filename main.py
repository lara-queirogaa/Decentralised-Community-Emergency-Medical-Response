"""
Lançador da simulação de resposta médica de emergência descentralizada.

Lê um cenário de um ficheiro JSON, constrói o ambiente e os agentes SPADE
descritos nele, provoca o acidente (se existir) e corre a simulação.

Utilização:
    python main.py scenarios/cenario_01_paragem.json

O JSON é a única fonte de verdade de um cenário: mapa, eventos do mundo
(trânsito e estradas cortadas), parâmetros dos agentes, acidente e duração.
Para testar outra situação basta escrever outro JSON e passá-lo a este
script, sem mexer no código.
"""

import argparse
import asyncio
import json
from pathlib import Path

from Ambulance import AmbulanceAgent
from Dispatcher import DispatcherAgent
from Environment import Environment, WITNESS_RADIUS
from Police import PoliceAgent
from Smartwatch import SmartwatchAgent
from Verification import VerificationAgent
from Witness import WitnessAgent


SECCOES_OBRIGATORIAS = ("server", "map", "dispatcher", "verification")
SEGUNDOS_ANTES_ACIDENTE = 3
TIPOS_ACIDENTE = ("smartwatch", "witness")


# ----------------------------------------------------------------------
# Auxiliares
# ----------------------------------------------------------------------

def jid(name, domain):
    """
    Constrói o JID de um agente.

    Args:
        name (str): Nome local do agente, por exemplo "ambulance1".
        domain (str): Domínio do servidor XMPP, por exemplo "localhost".

    Returns:
        str: O JID, por exemplo "ambulance1@localhost".
    """
    return f"{name}@{domain}"


def pos(value):
    """
    Converte uma posição do JSON, [x, y], num tuplo (x, y).

    Args:
        value (list[int]): Posição tal como vem do ficheiro JSON.

    Returns:
        tuple[int, int]: A mesma posição, como tuplo.
    """
    return tuple(value)


def load_scenario(path):
    """
    Lê um cenário de um ficheiro JSON.

    Args:
        path (Path): Caminho do ficheiro do cenário.

    Returns:
        dict: O cenário lido.

    Raises:
        FileNotFoundError: Se o ficheiro não existir.
        json.JSONDecodeError: Se o ficheiro não for JSON válido.
    """
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_scenario(cfg):
    """
    Verifica se o cenário tem as secções obrigatórias e se é coerente.

    Args:
        cfg (dict): O cenário lido.

    Raises:
        KeyError: Se faltar uma secção obrigatória.
        ValueError: Se o tipo de acidente for inválido ou se o acidente
            referir um smartwatch que não existe.
    """
    for key in SECCOES_OBRIGATORIAS:
        if key not in cfg:
            raise KeyError(f"O cenário não tem a secção '{key}'.")

    accident = cfg.get("accident")
    if not accident:
        return

    tipo = accident.get("type")
    if tipo not in TIPOS_ACIDENTE:
        raise ValueError(
            f"Tipo de acidente inválido: '{tipo}'. "
            f"Usa um de {TIPOS_ACIDENTE}."
        )

    if tipo == "smartwatch":
        sensor_ids = {s["sensor_id"] for s in cfg.get("smartwatches", [])}
        if accident.get("smartwatch") not in sensor_ids:
            raise ValueError(
                f"O acidente refere um smartwatch desconhecido: "
                f"'{accident.get('smartwatch')}'."
            )


# ----------------------------------------------------------------------
# Construção (JSON -> objetos)
# ----------------------------------------------------------------------

def build_environment(cfg):
    """
    Cria o ambiente (mapa e hospitais) do cenário.

    Args:
        cfg (dict): O cenário lido.

    Returns:
        Environment: O ambiente, ainda sem agentes.
    """
    m = cfg["map"]
    return Environment(
        m["width"], m["height"],
        hospitals=[pos(h) for h in m["hospitals"]]
    )


def build_agent_infos(cfg):
    """
    Cria as descrições das testemunhas, ambulâncias e polícias.

    São estes dicionários que o dispatcher e o agente de verificação
    recebem para saberem que agentes existem e onde estão.

    Args:
        cfg (dict): O cenário lido.

    Returns:
        tuple[list[dict], list[dict], list[dict]]: As descrições das
            testemunhas, das ambulâncias e dos polícias, por esta ordem.
    """
    domain = cfg["server"]["domain"]

    witnesses = [
        {"jid": jid(w["name"], domain), "witness_id": w["witness_id"],
         "location": pos(w["location"])}
        for w in cfg.get("witnesses", [])
    ]
    ambulances = [
        {"unit_id": a["unit_id"], "jid": jid(a["name"], domain),
         "location": pos(a["location"])}
        for a in cfg.get("ambulances", [])
    ]
    police = [
        {"unit_id": p["unit_id"], "jid": jid(p["name"], domain),
         "location": pos(p["location"])}
        for p in cfg.get("police", [])
    ]
    return witnesses, ambulances, police


def build_dispatcher(cfg, witness_infos, ambulance_infos, police_infos):
    """
    Cria o dispatcher (não vive no mapa).

    Args:
        cfg (dict): O cenário lido.
        witness_infos (list[dict]): Descrições das testemunhas.
        ambulance_infos (list[dict]): Descrições das ambulâncias.
        police_infos (list[dict]): Descrições dos polícias.

    Returns:
        DispatcherAgent: O dispatcher, ainda por arrancar.
    """
    domain = cfg["server"]["domain"]
    d = cfg["dispatcher"]
    return DispatcherAgent(
        jid("dispatcher", domain),
        cfg["server"]["password"],
        witnesses=witness_infos,
        ambulances=ambulance_infos,
        police=police_infos,
        max_witness_distance=d["max_witness_distance"],
        cpr_timeout=d["cpr_timeout"]
    )


def build_verification(cfg, witness_infos):
    """
    Cria o agente de verificação (não vive no mapa).

    Args:
        cfg (dict): O cenário lido.
        witness_infos (list[dict]): Descrições das testemunhas.

    Returns:
        VerificationAgent: O agente de verificação, ainda por arrancar.
    """
    domain = cfg["server"]["domain"]
    v = cfg["verification"]
    return VerificationAgent(
        jid=jid("verification", domain),
        password=cfg["server"]["password"],
        dispatcher_jid=jid("dispatcher", domain),
        witnesses=witness_infos,
        max_distance=v["max_distance"],
        threshold=v["threshold"]
    )


def build_ambulances(cfg):
    """
    Cria as ambulâncias descritas no cenário.

    Args:
        cfg (dict): O cenário lido.

    Returns:
        list[AmbulanceAgent]: As ambulâncias, ainda por arrancar.
    """
    domain = cfg["server"]["domain"]
    return [
        AmbulanceAgent(
            jid=jid(a["name"], domain),
            password=cfg["server"]["password"],
            unit_id=a["unit_id"],
            hospital_location=pos(a["location"]),
            dispatcher_jid=jid("dispatcher", domain)
        )
        for a in cfg.get("ambulances", [])
    ]


def build_police(cfg):
    """
    Cria as viaturas de polícia descritas no cenário.

    Args:
        cfg (dict): O cenário lido.

    Returns:
        list[PoliceAgent]: As viaturas, ainda por arrancar.
    """
    domain = cfg["server"]["domain"]
    return [
        PoliceAgent(
            jid=jid(p["name"], domain),
            password=cfg["server"]["password"],
            unit_id=p["unit_id"],
            patrol_location=pos(p["location"]),
            dispatcher_jid=jid("dispatcher", domain)
        )
        for p in cfg.get("police", [])
    ]


def build_witnesses(cfg):
    """
    Cria as testemunhas descritas no cenário.

    Args:
        cfg (dict): O cenário lido.

    Returns:
        list[WitnessAgent]: As testemunhas, ainda por arrancar.
    """
    domain = cfg["server"]["domain"]
    return [
        WitnessAgent(
            jid=jid(w["name"], domain),
            password=cfg["server"]["password"],
            witness_id=w["witness_id"],
            location=pos(w["location"]),
            verification_jid=jid("verification", domain),
            dispatcher_jid=jid("dispatcher", domain),
            confidence=w.get("confidence", 0.9),
            detect_on_start=w.get("detect_on_start", False),
            confirm_requests=w.get("confirm_requests", True),
            accept_cpr=w.get("accept_cpr", True)
        )
        for w in cfg.get("witnesses", [])
    ]


def build_smartwatches(cfg):
    """
    Cria os smartwatches descritos no cenário.

    Args:
        cfg (dict): O cenário lido.

    Returns:
        dict[str, SmartwatchAgent]: Os smartwatches, indexados por
            sensor_id, ainda por arrancar.
    """
    domain = cfg["server"]["domain"]
    return {
        s["sensor_id"]: SmartwatchAgent(
            jid=jid(s["name"], domain),
            password=cfg["server"]["password"],
            sensor_id=s["sensor_id"],
            location=pos(s["location"]),
            verification_jid=jid("verification", domain),
            dispatcher_jid=jid("dispatcher", domain),
            confidence=s.get("confidence", 0.95)
        )
        for s in cfg.get("smartwatches", [])
    }


# ----------------------------------------------------------------------
# Passos da simulação
# ----------------------------------------------------------------------

async def start_agents(
    env, dispatcher, verification,
    ambulances, police, witnesses, smartwatches
):
    """
    Arranca todos os agentes.

    O dispatcher e o agente de verificação não vivem no mapa, por isso
    arrancam diretamente. Os restantes são registados e arrancados pelo
    ambiente.

    Args:
        env (Environment): O ambiente.
        dispatcher (DispatcherAgent): O dispatcher.
        verification (VerificationAgent): O agente de verificação.
        ambulances (list[AmbulanceAgent]): As ambulâncias.
        police (list[PoliceAgent]): As viaturas de polícia.
        witnesses (list[WitnessAgent]): As testemunhas.
        smartwatches (dict[str, SmartwatchAgent]): Os smartwatches.
    """
    await dispatcher.start()
    await verification.start()

    for a in ambulances:
        await env.add_agent("ambulance", a)
    for p in police:
        await env.add_agent("police", p)
    for w in witnesses:
        await env.add_agent("witness", w)
    for sw in smartwatches.values():
        await env.add_agent("smartwatch", sw)

    print("\nTodos os agentes foram iniciados.")


def apply_world_events(env, cfg):
    """
    Aplica as condições do mundo (estradas cortadas e trânsito).

    Args:
        env (Environment): O ambiente.
        cfg (dict): O cenário lido.
    """
    world = cfg.get("world", {})

    for position in world.get("roadblocks", []):
        env.add_roadblock(pos(position))

    if world.get("traffic_level", 0):
        env.set_traffic(world["traffic_level"])


async def trigger_accident(env, cfg, smartwatches):
    """
    Provoca o acidente do cenário, se existir.

    Com type "smartwatch", o smartwatch da vítima deteta o acidente (e, se
    real for false, é um falso alarme). Com type "witness", é uma testemunha
    que o vê e reporta.

    Args:
        env (Environment): O ambiente.
        cfg (dict): O cenário lido.
        smartwatches (dict[str, SmartwatchAgent]): Os smartwatches,
            indexados por sensor_id.
    """
    accident = cfg.get("accident")

    if not accident:
        print("\nSem acidente neste cenário.")
        return

    position = pos(accident["position"])
    vitals = accident.get("vitals")

    if accident["type"] == "smartwatch":
        await env.trigger_smartwatch_accident(
            smartwatches[accident["smartwatch"]],
            vitals=vitals,
            position=position,
            real=accident.get("real", True)
        )
    else:
        await env.trigger_witness_accident(
            position,
            vitals,
            radius=accident.get("radius", WITNESS_RADIUS)
        )


async def wait_for_end(env, cfg):
    """
    Mantém a simulação a correr até ao fim da duração configurada.

    Se run.duration_seconds for null, a simulação corre até ser
    interrompida com Ctrl+C.

    Args:
        env (Environment): O ambiente (usado para mostrar o mapa final).
        cfg (dict): O cenário lido.
    """
    duration = cfg.get("run", {}).get("duration_seconds")

    if duration is None:
        while True:
            await asyncio.sleep(1)

    await asyncio.sleep(duration)
    print("\nMapa final:")
    print(env.render())


async def stop_agents(agents):
    """
    Pára os agentes que ainda estão vivos, pela ordem inversa do arranque.

    Args:
        agents (list): Todos os agentes criados.
    """
    for agent in reversed(agents):
        if agent.is_alive():
            await agent.stop()


# ----------------------------------------------------------------------
# Pontos de entrada
# ----------------------------------------------------------------------

async def run(cfg):
    """
    Constrói e corre a simulação completa de um cenário.

    Args:
        cfg (dict): O cenário lido.

    Raises:
        KeyError: Se faltar uma secção obrigatória no cenário.
        ValueError: Se o cenário for incoerente.
    """
    validate_scenario(cfg)

    print(f"\n=== Cenário: {cfg.get('name', 'sem nome')} ===")
    if cfg.get("description"):
        print(cfg["description"])

    env = build_environment(cfg)
    witness_infos, ambulance_infos, police_infos = build_agent_infos(cfg)

    dispatcher = build_dispatcher(
        cfg, witness_infos, ambulance_infos, police_infos
    )
    verification = build_verification(cfg, witness_infos)
    ambulances = build_ambulances(cfg)
    police = build_police(cfg)
    witnesses = build_witnesses(cfg)
    smartwatches = build_smartwatches(cfg)

    try:
        await start_agents(
            env, dispatcher, verification,
            ambulances, police, witnesses, smartwatches
        )

        apply_world_events(env, cfg)
        print("\nMapa inicial:")
        print(env.render())

        await asyncio.sleep(
            cfg.get("run", {}).get(
                "seconds_before_accident", SEGUNDOS_ANTES_ACIDENTE
            )
        )

        await trigger_accident(env, cfg, smartwatches)
        await wait_for_end(env, cfg)
    finally:
        await stop_agents([dispatcher, verification] + env.all_agents())


def main():
    """
    Lê a linha de comandos, carrega o cenário e corre-o.
    """
    parser = argparse.ArgumentParser(
        description="Simulação de resposta médica de emergência (SPADE)."
    )
    parser.add_argument(
        "scenario", type=Path, help="caminho do JSON do cenário"
    )
    args = parser.parse_args()

    cfg = load_scenario(args.scenario)

    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\nTerminado.")


if __name__ == "__main__":
    main()