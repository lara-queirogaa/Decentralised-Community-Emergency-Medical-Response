import asyncio
import json
import math

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour
from spade.message import Message


async def send_json(behaviour, jid, data):
    msg = Message(to=str(jid))
    msg.set_metadata("performative", "inform")
    msg.body = json.dumps(data)
    await behaviour.send(msg)


class AmbulanceAgent(Agent):
    """Ambulância: só obedece ao dispatcher.
    Quando está livre está sempre no hospital."""

    # Preenchido pelo Environment.add_agent
    environment = None

    def __init__(
        self, jid, password, unit_id, hospital_location,
        dispatcher_jid, speed=2.0, scene_time=5.0
    ):
        super().__init__(jid, password)
        self.unit_id = unit_id
        self.hospital_location = tuple(hospital_location)
        self.location = self.hospital_location
        self.dispatcher_jid = dispatcher_jid
        self.speed = speed            # unidades de distância por segundo
        self.scene_time = scene_time  # segundos no local
        self.available = True
        self.current_incident = None
        self.cancelled = False

    def travel_time(self, origin, destination):
        """Segundos de viagem. Com ambiente, segue a rota real
        (estradas cortadas e trânsito); senão, linha reta."""
        if self.environment is not None:
            needed = self.environment.travel_time(
                origin, destination, self.speed
            )
            if not math.isinf(needed):
                return needed
            print(f"[{self.unit_id}] Sem rota. A usar linha reta.")

        return math.dist(origin, destination) / self.speed

    def status_message(self, status, incident_id=None):
        return {
            "message_type": "unit_status",
            "unit_id": self.unit_id,
            "kind": "ambulance",
            "status": status,
            "available": self.available,
            "incident_id": incident_id,
            "location": [
                round(self.location[0], 2),
                round(self.location[1], 2)
            ]
        }

    async def wait_or_cancel(self, seconds):
        """Espera 'seconds', mas pára se o dispatcher cancelar.
        Devolve (terminou_sem_cancelar, segundos_decorridos)."""
        elapsed = 0.0

        while elapsed < seconds:
            if self.cancelled:
                return False, elapsed

            step = min(0.2, seconds - elapsed)
            await asyncio.sleep(step)
            elapsed += step

        return not self.cancelled, elapsed

    class ReceiveOrdersBehaviour(CyclicBehaviour):

        async def run(self):
            msg = await self.receive(timeout=1)
            if msg is None:
                return

            try:
                data = json.loads(msg.body)
            except (json.JSONDecodeError, TypeError):
                return

            agent = self.agent
            kind = data.get("message_type")
            incident_id = data.get("incident_id")

            if kind == "dispatch_order":
                location = data.get("location")

                if not incident_id or not location:
                    return

                if agent.current_incident == incident_id:
                    return  # ordem repetida

                if not agent.available:
                    print(
                        f"\n--- AMBULANCE {agent.unit_id} ---\n"
                        f"Ocupada. Ordem para {incident_id} recusada."
                    )
                    await send_json(
                        self,
                        agent.dispatcher_jid,
                        agent.status_message("rejected", incident_id)
                    )
                    return

                agent.available = False
                agent.current_incident = incident_id
                agent.cancelled = False

                print(f"\n--- AMBULANCE {agent.unit_id} ---")
                print(f"Ordem recebida para o incidente {incident_id}.")

                agent.add_behaviour(
                    agent.MissionBehaviour(
                        incident_id, tuple(location)
                    )
                )

            elif kind == "dispatch_cancel":
                if (
                    incident_id
                    and incident_id == agent.current_incident
                ):
                    agent.cancelled = True
                    print(f"\n--- AMBULANCE {agent.unit_id} ---")
                    print(
                        f"Missão {incident_id} cancelada. "
                        f"A regressar ao hospital."
                    )

    class MissionBehaviour(OneShotBehaviour):

        def __init__(self, incident_id, destination):
            super().__init__()
            self.incident_id = incident_id
            self.destination = destination

        async def report(self, status):
            agent = self.agent
            await send_json(
                self,
                agent.dispatcher_jid,
                agent.status_message(status, self.incident_id)
            )

        async def run(self):
            agent = self.agent

            await self.report("en_route")
            print(f"[{agent.unit_id}] A caminho do local.")

            travel = agent.travel_time(
                agent.location, self.destination
            )
            ok, elapsed = await agent.wait_or_cancel(travel)

            if ok:
                agent.location = self.destination
                print(f"[{agent.unit_id}] Chegou ao local.")
                await self.report("on_scene")

                await agent.wait_or_cancel(agent.scene_time)

                return_time = agent.travel_time(
                    self.destination, agent.hospital_location
                )
            else:
                # Cancelada a meio da viagem: volta pelo mesmo caminho.
                return_time = elapsed

            await self.report("returning")
            print(f"[{agent.unit_id}] A regressar ao hospital.")
            await asyncio.sleep(return_time)

            agent.location = agent.hospital_location
            agent.available = True
            agent.current_incident = None
            agent.cancelled = False

            print(f"[{agent.unit_id}] No hospital. Disponível.")
            await self.report("available")

    async def setup(self):
        print(f"Ambulance {self.unit_id} iniciada.")
        self.add_behaviour(self.ReceiveOrdersBehaviour())