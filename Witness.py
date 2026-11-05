
import json
import uuid

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour, CyclicBehaviour
from spade.message import Message


class WitnessAgent(Agent):

    def __init__(
        self, jid, password, witness_id, location,
        verification_jid, dispatcher_jid=None, confidence=0.9,
        detect_on_start=False, confirm_requests=True, accept_cpr=True
    ):
        super().__init__(jid, password)
        self.witness_id = witness_id
        self.location = location
        self.verification_jid = verification_jid
        self.confidence = confidence
        self.detect_on_start = detect_on_start
        self.confirm_requests = confirm_requests
        self.hits = 0
        self.false_alarms = 0
        self.processed_feedback = set()
        self.dispatcher_jid = dispatcher_jid
        self.accept_cpr = accept_cpr

    def get_reputation(self):
        return (self.hits + 1) / (
            self.hits + self.false_alarms + 2
        )

    class DetectIncidentBehaviour(OneShotBehaviour):

        async def run(self):
            agent = self.agent
            incident_id = str(uuid.uuid4())

            incident = {
                "message_type": "incident",
                "incident_id": incident_id,
                "origin": "witness",
                "witness_id": agent.witness_id,
                "source_id": agent.witness_id,
                "confidence": agent.confidence,
                "reputation": agent.get_reputation(),
                "urgent": agent.confidence >= 0.9,
                "type": "possible_accident",
                "location": [
                    round(agent.location[0], 1),
                    round(agent.location[1], 1)
                ]
            }

            print("\n--- WITNESS ---")
            print("Possível acidente detetado.")
            print(f"Confiança: {agent.confidence:.2f}")
            print(f"Reputação: {agent.get_reputation():.2f}")

            msg = Message(to=str(agent.verification_jid))
            msg.set_metadata("performative", "inform")
            msg.body = json.dumps(incident)
            await self.send(msg)

    class ReceiveMessagesBehaviour(CyclicBehaviour):

        async def run(self):
            msg = await self.receive(timeout=1)
            if msg is None:
                return

            try:
                data = json.loads(msg.body)
            except (json.JSONDecodeError, TypeError):
                return

            agent = self.agent
            message_type = data.get("message_type")

            if message_type == "verify_request":
                response = {
                    "message_type": "witness_response",
                    "incident_id": data["incident_id"],
                    "witness_id": agent.witness_id,
                    "confirmed": agent.confirm_requests,
                    "confidence": agent.confidence,
                    "reputation": agent.get_reputation(),
                    "location": [
                        round(agent.location[0], 1),
                        round(agent.location[1], 1)
                    ]
                }

                reply = Message(to=str(msg.sender))
                reply.set_metadata("performative", "inform")
                reply.body = json.dumps(response)
                await self.send(reply)

                print("\n--- WITNESS ---")
                print(f"Pedido recebido: {data['incident_id']}")
                print(f"Confirma: {agent.confirm_requests}")

            elif message_type == "reputation_feedback":
                if data.get("source_id") != agent.witness_id:
                    return

                incident_id = data.get("incident_id")
                if (
                    not incident_id
                    or incident_id in agent.processed_feedback
                ):
                    return

                agent.processed_feedback.add(incident_id)

                if data.get("outcome") == "confirmed":
                    agent.hits += 1
                elif data.get("outcome") == "false_alarm":
                    agent.false_alarms += 1
                else:
                    return

                print("\n--- REPUTAÇÃO DA WITNESS ---")
                print(f"Reputação: {agent.get_reputation():.2f}")
                
            elif message_type == "cpr_request":
                response = {
                    "message_type": "cpr_response",
                    "incident_id": data["incident_id"],
                    "witness_id": agent.witness_id,
                    "accepted": agent.accept_cpr
                }
                reply = Message(to=str(msg.sender))
                reply.set_metadata("performative", "inform")
                reply.body = json.dumps(response)
                await self.send(reply)

                print("\n--- WITNESS ---")
                if agent.accept_cpr:
                    print("A iniciar RCP. Instruções:")
                    for step in data.get("instructions", []):
                        print(f"  - {step}")
                else:
                    print("Não posso fazer RCP.")

            elif message_type == "cpr_cancel":
                print("\n--- WITNESS ---")
                print("RCP cancelada pelo dispatcher (falso alarme).")

    async def setup(self):
        print(f"Witness {self.witness_id} iniciado.")
        self.add_behaviour(self.ReceiveMessagesBehaviour())

        if self.detect_on_start:
            self.add_behaviour(self.DetectIncidentBehaviour())
