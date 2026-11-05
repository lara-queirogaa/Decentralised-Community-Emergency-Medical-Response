import asyncio

from Ambulance import AmbulanceAgent
from Dispatcher import DispatcherAgent
from Environment import Environment
from Police import PoliceAgent
from Smartwatch import SmartwatchAgent
from Verification import VerificationAgent
from Witness import WitnessAgent


# ----------------------------------------------------------------------
# Configuração do cenário (todas as posições são (x, y) numa estrada)
# ----------------------------------------------------------------------

DOMAIN = "localhost"
PASSWORD = "password123"

HOSPITALS = [(0, 0), (12, 12)]

ACCIDENT_POSITION = (4, 4)
ACCIDENT_VITALS = {"heart_rate": 0, "breathing": False}  # paragem
# ACCIDENT_VITALS = {"heart_rate": 80, "breathing": True}  # sem RCP

TRAFFIC_LEVEL = 0        # 0 = sem trânsito, 1 a 5 = cada vez mais
ROADBLOCKS = []          # ex.: [(4, 2), (6, 3)]

SECONDS_BEFORE_ACCIDENT = 3


def jid(name):
    return f"{name}@{DOMAIN}"


async def main():
    env = Environment(16, 16, hospitals=HOSPITALS)

    witness_info = {
        "jid": jid("witness1"),
        "witness_id": "witness_001",
        "location": (4, 3)
    }

    # --- Agentes sem posição no mapa -------------------------------
    dispatcher = DispatcherAgent(
        jid("dispatcher"),
        PASSWORD,
        witnesses=[witness_info],
        ambulances=[
            {"unit_id": "amb_01", "jid": jid("ambulance1"),
             "location": (0, 0)},
            {"unit_id": "amb_02", "jid": jid("ambulance2"),
             "location": (12, 12)}
        ],
        police=[
            {"unit_id": "pol_01", "jid": jid("police1"),
             "location": (8, 4)},
            {"unit_id": "pol_02", "jid": jid("police2"),
             "location": (4, 8)}
        ],
        max_witness_distance=5.0,
        cpr_timeout=10
    )

    verification = VerificationAgent(
        jid=jid("verification"),
        password=PASSWORD,
        dispatcher_jid=jid("dispatcher"),
        witnesses=[witness_info],
        max_distance=5.0,
        threshold=0.65
    )

    # --- Agentes com posição no mapa -------------------------------
    ambulance1 = AmbulanceAgent(
        jid=jid("ambulance1"), password=PASSWORD,
        unit_id="amb_01", hospital_location=(0, 0),
        dispatcher_jid=jid("dispatcher")
    )
    ambulance2 = AmbulanceAgent(
        jid=jid("ambulance2"), password=PASSWORD,
        unit_id="amb_02", hospital_location=(12, 12),
        dispatcher_jid=jid("dispatcher")
    )

    police1 = PoliceAgent(
        jid=jid("police1"), password=PASSWORD,
        unit_id="pol_01", patrol_location=(8, 4),
        dispatcher_jid=jid("dispatcher")
    )
    police2 = PoliceAgent(
        jid=jid("police2"), password=PASSWORD,
        unit_id="pol_02", patrol_location=(4, 8),
        dispatcher_jid=jid("dispatcher")
    )

    witness = WitnessAgent(
        jid=witness_info["jid"],
        password=PASSWORD,
        witness_id=witness_info["witness_id"],
        location=witness_info["location"],
        verification_jid=jid("verification"),
        dispatcher_jid=jid("dispatcher"),
        confidence=0.9,
        detect_on_start=False,
        confirm_requests=True,
        accept_cpr=True
    )

    smartwatch = SmartwatchAgent(
        jid=jid("smartwatch1"),
        password=PASSWORD,
        sensor_id="sensor_001",
        location=ACCIDENT_POSITION,
        verification_jid=jid("verification"),
        dispatcher_jid=jid("dispatcher"),
        confidence=0.95
    )

    try:
        # Dispatcher e verification não vivem no mapa
        await dispatcher.start()
        await verification.start()

        # O resto é registado e arrancado pelo ambiente
        await env.add_agent("ambulance", ambulance1)
        await env.add_agent("ambulance", ambulance2)
        await env.add_agent("police", police1)
        await env.add_agent("police", police2)
        await env.add_agent("witness", witness)
        await env.add_agent("smartwatch", smartwatch)

        print("\nTodos os agentes foram iniciados.")

        # Eventos do mundo (opcionais)
        for position in ROADBLOCKS:
            env.add_roadblock(position)
        if TRAFFIC_LEVEL:
            env.set_traffic(TRAFFIC_LEVEL)

        print("\nMapa inicial:")
        print(env.render())
        await asyncio.sleep(SECONDS_BEFORE_ACCIDENT)

        # O mundo provoca o acidente; o smartwatch deteta-o
        await env.trigger_accident(
            smartwatch, ACCIDENT_POSITION, vitals=ACCIDENT_VITALS
        )

        while True:
            await asyncio.sleep(1)

    finally:
        for agent in reversed(
            [dispatcher, verification] + env.all_agents()
        ):
            if agent.is_alive():
                await agent.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nTerminado.")