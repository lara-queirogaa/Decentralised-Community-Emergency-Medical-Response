import asyncio
import json
import math
import time

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.message import Message

WITNESS_WEIGHT = 0.7
SENSOR_WEIGHT = 0.3

class VerificationAgent(Agent):
    def __init__(
        self, jid, password, dispatcher_jid,
        witnesses, max_distance=5.0, threshold=0.65
    ):
        super().__init__(jid, password)
        self.dispatcher_jid = dispatcher_jid
        self.witnesses = witnesses
        self.max_distance = max_distance
        self.threshold = threshold
        self.stats = {}
        self.pending = {}
        self.finalized = set()

    def get_reputation(self, source_id):
        stats = self.stats.get(
            source_id, {"hits": 0, "false_alarms": 0}
        )
        hits = stats["hits"]
        false_alarms = stats["false_alarms"]
        return (hits + 1) / (hits + false_alarms + 2)

    def record_outcome(self, source_id, outcome):
        if source_id not in self.stats:
            self.stats[source_id] = {
                "hits": 0,
                "false_alarms": 0
            }

        if outcome == "confirmed":
            self.stats[source_id]["hits"] += 1
        else:
            self.stats[source_id]["false_alarms"] += 1

    def find_nearest_witness(self, location):
        nearest = None
        nearest_distance = float("inf")

        for witness in self.witnesses:
            distance = math.dist(
                location, witness["location"]
            )

            if distance < nearest_distance:
                nearest = witness
                nearest_distance = distance

        if nearest and nearest_distance <= self.max_distance:
            return nearest, nearest_distance

        return None, nearest_distance

    class ReceiveMessagesBehaviour(CyclicBehaviour):
        
        async def send_feedback(
            self, jid, source_id, incident_id, outcome
        ):
            feedback = {
                "message_type": "reputation_feedback",
                "incident_id": incident_id,
                "source_id": source_id,
                "outcome": outcome
            }

            msg = Message(to=str(jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(feedback)
            await self.send(msg)

        async def send_dispatcher_message(self, data):
            msg = Message(to=str(self.agent.dispatcher_jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(data)
            await self.send(msg)
    
        async def run(self):
            agent = self.agent
            msg = await self.receive(timeout=1)

            if msg is not None:
                try:
                    data = json.loads(msg.body)
                except (json.JSONDecodeError, TypeError):
                    data = None

                if data:
                    kind = data.get("message_type")

                    if kind == "incident":
                        await self.process_incident(
                            data, str(msg.sender)
                        )

                    elif kind == "witness_response":
                        await self.process_witness_response(data)

            now = time.monotonic()

            for incident_id, pending in list(
                agent.pending.items()
            ):
                if now - pending["sent_at"] >= 8:
                    del agent.pending[incident_id]

                    print(
                        f"\nSem resposta da testemunha para "
                        f"{incident_id}. A decidir com o smartwatch."
                    )

                    incident = pending["incident"]
                    sensor_id = incident["source_id"]
                    sensor_rep = agent.get_reputation(sensor_id)
                    confidence = incident["confidence"]

                    score = confidence * (0.5 + 0.5 * sensor_rep)

                    await self.finalize_incident(
                        incident=incident,
                        score=score,
                        source_jid=pending["source_jid"],
                        witness_response=None,
                        witness_jid=None
                    )

        async def process_incident(self, incident, sender_jid):
            agent = self.agent
            incident_id = incident.get("incident_id")

            if not incident_id or incident_id in agent.finalized:
                return

            origin = incident.get("origin")
            source_id = incident.get("source_id")
            confidence = float(incident.get("confidence", 0.0))
            location = incident.get("location", [0, 0])

            print("\n--- VERIFICATION ---")
            print(f"Incidente: {incident_id}")
            print(f"Origem: {origin}")

            if origin == "witness":
                reputation = agent.get_reputation(source_id)
                score = confidence * (0.5 + 0.5 * reputation)

                await self.finalize_incident(
                    incident=incident,
                    score=score,
                    source_jid=sender_jid,
                    witness_response=None,
                    witness_jid=None
                )
                return

            witness, distance = agent.find_nearest_witness(location)

            if witness is None:
                reputation = agent.get_reputation(source_id)
                score = confidence * (0.5 + 0.5 * reputation)

                print("Não há testemunha próxima.")
                await self.finalize_incident(
                    incident=incident,
                    score=score,
                    source_jid=sender_jid,
                    witness_response=None,
                    witness_jid=None
                )
                return

            print(
                f"A pedir verificação a {witness['witness_id']}; "
                f"distância: {distance:.2f}."
            )

            agent.pending[incident_id] = {
                "incident": incident,
                "source_jid": sender_jid,
                "witness": witness,
                "sent_at": time.monotonic()
            }

            request = {
                "message_type": "verify_request",
                "incident_id": incident_id,
                "location": location
            }

            request_msg = Message(to=witness["jid"])
            request_msg.set_metadata("performative", "inform")
            request_msg.body = json.dumps(request)
            await self.send(request_msg)

        async def process_witness_response(self, response):
            agent = self.agent
            incident_id = response.get("incident_id")
            pending = agent.pending.pop(incident_id, None)

            if pending is None or incident_id in agent.finalized:
                return

            incident = pending["incident"]
            sensor_id = incident["source_id"]
            sensor_rep = agent.get_reputation(sensor_id)
            sensor_conf = float(incident.get("confidence", 0.0))

            witness_id = response["witness_id"]
            witness_rep = agent.get_reputation(witness_id)
            witness_conf = float(response.get("confidence", 0.0))

            sensor_reliability = 0.5 + 0.5 * sensor_rep
            witness_reliability = 0.5 + 0.5 * witness_rep

            sensor_p = sensor_conf

            if response.get("confirmed"):
                witness_p = witness_conf
            else:
                witness_p = 1 - witness_conf

            sensor_w = SENSOR_WEIGHT * sensor_reliability
            witness_w = WITNESS_WEIGHT * witness_reliability

            score = (
                sensor_w * sensor_p + witness_w * witness_p
            ) / (sensor_w + witness_w)

            print("\n--- RESULTADO DA VERIFICAÇÃO ---")
            print(f"Reputação smartwatch: {sensor_rep:.2f}")
            print(f"Reputação witness: {witness_rep:.2f}")
            print(f"Pontuação final: {score:.3f}")
            print(f"Limiar: {agent.threshold:.2f}")
            print(f"Testemunha confirmou: {response['confirmed']}")

            witness_jid = pending["witness"]["jid"]

            await self.finalize_incident(
                incident=incident,
                score=score,
                source_jid=pending["source_jid"],
                witness_response=response,
                witness_jid=witness_jid
            )

        async def finalize_incident(
            self, incident, score, source_jid,
            witness_response, witness_jid
        ):
            agent = self.agent
            incident_id = incident["incident_id"]

            if incident_id in agent.finalized:
                return

            agent.finalized.add(incident_id)

            confirmed = score >= agent.threshold
            outcome = "confirmed" if confirmed else "false_alarm"
            source_id = incident["source_id"]
            origin = incident.get("origin")

            agent.record_outcome(source_id, outcome)

            await self.send_feedback(
                source_jid, source_id, incident_id, outcome
            )

            if witness_response is not None:
                witness_id = witness_response["witness_id"]

                # Uma testemunha que negou o alerta de um
                # smartwatch não é penalizada nem recompensada.
                if witness_response.get("confirmed"):
                    agent.record_outcome(witness_id, outcome)

                    await self.send_feedback(
                        witness_jid,
                        witness_id,
                        incident_id,
                        outcome
                    )

            if confirmed:
                print(f"Incidente confirmado: {incident_id}")

                result = {
                    **incident,
                    "message_type": "confirmed_incident",
                    "verification_score": round(score, 3)
                }
                await self.send_dispatcher_message(result)

            else:
                print(f"Falso alarme: {incident_id}")

                # Só cancelar no dispatcher se o alerta urgente
                # já tiver sido enviado pelo smartwatch.
                if incident.get("urgent"):
                    cancellation = {
                        "message_type": "cancel_alert",
                        "incident_id": incident_id,
                        "reason": "false_alarm",
                        "verification_score": round(score, 3)
                    }
                    await self.send_dispatcher_message(cancellation)

            print(
                f"Reputação de {source_id}: "
                f"{agent.get_reputation(source_id):.2f}"
            )

            if witness_response is not None:
                witness_id = witness_response["witness_id"]
                print(
                    f"Reputação de {witness_id}: "
                    f"{agent.get_reputation(witness_id):.2f}"
                )

    async def setup(self):
        print("VerificationAgent iniciado.")
        self.add_behaviour(self.ReceiveMessagesBehaviour())
