"""
Lançador da simulação de resposta médica de emergência descentralizada.

Lê um cenário de um ficheiro JSON, constrói o ambiente e os agentes SPADE
descritos nele, provoca os acidentes (se existirem) e corre a simulação.

Utilização:
    python main.py scenarios/cenario01.json

O JSON é a única fonte de verdade de um cenário: mapa, eventos do mundo
(trânsito e estradas cortadas), parâmetros dos agentes, acidente e duração.
Para testar outra situação basta escrever outro JSON e passá-lo a este
script, sem mexer no código.
"""

import argparse
import asyncio
import json
import random
from pathlib import Path

from Ambulance import AmbulanceAgent
from Dispatcher import DispatcherAgent
from Environment import Environment, WITNESS_RADIUS
from metrics import MetricsRecorder
from Police import PoliceAgent
from Smartwatch import SmartwatchAgent
from Verification import (
    MIN_RELIABILITY, SENSOR_WEIGHT, WITNESS_TIMEOUT, WITNESS_WEIGHT,
    VerificationAgent
)
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


def accident_list(cfg):
    """
    Devolve os acidentes de um cenário, como lista.

    Um cenário pode ter um só acidente ("accident") ou vários
    ("accidents"). Cada acidente pode ter "at": os segundos, a contar do
    primeiro acidente, a que acontece (por defeito 0).

    Args:
        cfg (dict): O cenário lido.

    Returns:
        list[dict]: Os acidentes, vazia se não houver nenhum.
    """
    if cfg.get("accidents"):
        return list(cfg["accidents"])
    if cfg.get("accident"):
        return [cfg["accident"]]
    return []


def validate_scenario(cfg):
    """
    Verifica se o cenário tem as secções obrigatórias e se é coerente.

    Args:
        cfg (dict): O cenário lido.

    Raises:
        KeyError: Se faltar uma secção obrigatória.
        ValueError: Se o tipo de acidente for inválido, se algum acidente
            referir um smartwatch que não existe ou se "at" for negativo.
    """
    for key in SECCOES_OBRIGATORIAS:
        if key not in cfg:
            raise KeyError(f"O cenário não tem a secção '{key}'.")

    sensor_ids = {s["sensor_id"] for s in cfg.get("smartwatches", [])}

    for accident in accident_list(cfg):
        tipo = accident.get("type")
        if tipo not in TIPOS_ACIDENTE:
            raise ValueError(
                f"Tipo de acidente inválido: '{tipo}'. "
                f"Usa um de {TIPOS_ACIDENTE}."
            )

        if tipo == "smartwatch" and (
            accident.get("smartwatch") not in sensor_ids
        ):
            raise ValueError(
                f"O acidente refere um smartwatch desconhecido: "
                f"'{accident.get('smartwatch')}'."
            )

        if accident.get("at", 0) < 0:
            raise ValueError("O campo 'at' de um acidente não pode ser negativo.")


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
        threshold=v["threshold"],
        witness_timeout=v.get("witness_timeout", WITNESS_TIMEOUT),
        witness_weight=v.get("witness_weight", WITNESS_WEIGHT),
        sensor_weight=v.get("sensor_weight", SENSOR_WEIGHT),
        min_reliability=v.get("min_reliability", MIN_RELIABILITY)
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
            dispatcher_jid=jid("dispatcher", domain),
            speed=a.get("speed", 2.0),
            scene_time=a.get("scene_time", 5.0)
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
            dispatcher_jid=jid("dispatcher", domain),
            speed=p.get("speed", 2.0),
            scene_time=p.get("scene_time", 5.0)
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
            accept_cpr=w.get("accept_cpr", True),
            error_rate=w.get("error_rate", 0.0),
            error_per_distance=w.get("error_per_distance", 0.0),
            urgent_threshold=w.get("urgent_threshold", 0.9),
            response_delay=w.get("response_delay", 0.0),
            response_delay_jitter=w.get("response_delay_jitter", 0.0),
            cpr_delay=w.get("cpr_delay", 0.0)
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
            confidence=s.get("confidence", 0.95),
            urgent_threshold=s.get("urgent_threshold", 0.9)
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


async def trigger_one_accident(env, accident, smartwatches):
    """
    Provoca um acidente.

    Com type "smartwatch", o smartwatch da vítima deteta o acidente (e, se
    real for false, é um falso alarme). Com type "witness", é uma testemunha
    que o vê e reporta (com real false, a testemunha engana-se e reporta um
    acidente que não existe).

    Args:
        env (Environment): O ambiente.
        accident (dict): O acidente, tal como vem do JSON.
        smartwatches (dict[str, SmartwatchAgent]): Os smartwatches,
            indexados por sensor_id.
    """
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
            radius=accident.get("radius", WITNESS_RADIUS),
            real=accident.get("real", True)
        )


async def trigger_accident(env, cfg, smartwatches):
    """
    Provoca os acidentes do cenário, se existirem.

    Cada acidente acontece "at" segundos depois do primeiro (0 por
    defeito), por ordem. Só regressa depois de provocar o último, por isso
    run.duration_seconds conta a partir daí.

    Args:
        env (Environment): O ambiente.
        cfg (dict): O cenário lido.
        smartwatches (dict[str, SmartwatchAgent]): Os smartwatches,
            indexados por sensor_id.
    """
    accidents = accident_list(cfg)

    if not accidents:
        print("\nSem acidente neste cenário.")
        return

    clock = asyncio.get_running_loop().time
    start = clock()

    for accident in sorted(accidents, key=lambda a: a.get("at", 0)):
        wait = accident.get("at", 0) - (clock() - start)
        if wait > 0:
            await asyncio.sleep(wait)
        await trigger_one_accident(env, accident, smartwatches)


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

async def run(cfg, metrics=None):
    """
    Constrói e corre a simulação completa de um cenário.

    Args:
        cfg (dict): O cenário lido.
        metrics (MetricsRecorder | None): Se for dado, fica ligado ao
            ambiente e à verificação para calcular as métricas no fim.
            Quem o chama é que o instala (install) e guarda os resultados.

    Raises:
        KeyError: Se faltar uma secção obrigatória no cenário.
        ValueError: Se o cenário for incoerente.
    """
    validate_scenario(cfg)

    # Com "seed" no JSON a simulação é reproduzível (trânsito, ruído das
    # testemunhas). Sem seed, cada corrida é diferente.
    seed = cfg.get("run", {}).get("seed")
    if seed is not None:
        random.seed(seed)

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

    # O dispatcher usa o ambiente para escolher unidades pela rota real
    # (trânsito e estradas cortadas) e a verificação para conhecer a verdade
    # do terreno quando atualiza as reputações.
    dispatcher.environment = env
    verification.environment = env

    if metrics is not None:
        metrics.attach(env, verification, dispatcher)

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

        if metrics is not None:
            metrics.mark_end()
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
    parser.add_argument(
        "--metrics", type=Path, metavar="PASTA",
        help="guarda as métricas da corrida em PASTA/runs.csv e "
             "PASTA/incidents.csv"
    )
    args = parser.parse_args()

    cfg = load_scenario(args.scenario)

    recorder = None
    if args.metrics:
        recorder = MetricsRecorder(
            args.scenario.stem, cfg.get("run", {}).get("seed")
        )
        recorder.install()

    try:
        asyncio.run(run(cfg, metrics=recorder))
    except KeyboardInterrupt:
        print("\nTerminado.")
    finally:
        if recorder is not None:
            recorder.uninstall()
            result = recorder.save(args.metrics)
            print(f"\nMétricas guardadas em {args.metrics}/")
            for key, value in result["run"].items():
                print(f"  {key}: {value}")


if __name__ == "__main__":
    main()