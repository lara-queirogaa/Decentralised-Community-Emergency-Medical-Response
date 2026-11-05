
import json
import uuid

from spade.agent import Agent
from spade.behaviour import OneShotBehaviour, CyclicBehaviour
from spade.message import Message


class SmartwatchAgent(Agent):

    def __init__(
        self, jid, password, sensor_id, location,
        verification_jid, dispatcher_jid, confidence=0.95, vitals=None
    ):
        super().__init__(jid, password)
        self.sensor_id = sensor_id
        self.location = location
        self.verification_jid = verification_jid
        self.dispatcher_jid = dispatcher_jid
        self.confidence = confidence
        self.hits = 0
        self.false_alarms = 0
        self.processed_feedback = set()
        self.vitals = vitals or {"heart_rate": 72, "breathing": True}

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
                "origin": "smartwatch",
                "sensor_id": agent.sensor_id,
                "source_id": agent.sensor_id,
                "confidence": agent.confidence,
                "reputation": agent.get_reputation(),
                "urgent": agent.confidence >= 0.9,
                "type": "possible_accident",
                "vitals": agent.vitals,
                "location": [
                    round(agent.location[0], 1),
                    round(agent.location[1], 1)
                ]
            }

            print("\n--- SMARTWATCH ---")
            print(f"Incidente: {incident_id}")
            print(f"Confiança: {agent.confidence:.2f}")
            print(f"Reputação: {agent.get_reputation():.2f}")
            print(f"Urgente: {incident['urgent']}")

            verification_msg = Message(
                to=str(agent.verification_jid)
            )
            verification_msg.set_metadata("performative", "inform")
            verification_msg.body = json.dumps(incident)
            await self.send(verification_msg)

            if incident["urgent"]:
                pre_alert = {
                    **incident,
                    "message_type": "urgent_pre_alert"
                }
                dispatcher_msg = Message(
                    to=str(agent.dispatcher_jid)
                )
                dispatcher_msg.set_metadata(
                    "performative", "inform"
                )
                dispatcher_msg.body = json.dumps(pre_alert)
                await self.send(dispatcher_msg)
                print("Pré-alerta urgente enviado ao dispatcher.")

    class ReceiveFeedbackBehaviour(CyclicBehaviour):

        async def run(self):
            msg = await self.receive(timeout=1)
            if msg is None:
                return

            try:
                data = json.loads(msg.body)
            except (json.JSONDecodeError, TypeError):
                return

            if data.get("message_type") != "reputation_feedback":
                return

            if data.get("source_id") != self.agent.sensor_id:
                return

            incident_id = data.get("incident_id")
            if not incident_id or incident_id in self.agent.processed_feedback:
                return

            self.agent.processed_feedback.add(incident_id)

            if data.get("outcome") == "confirmed":
                self.agent.hits += 1
            elif data.get("outcome") == "false_alarm":
                self.agent.false_alarms += 1
            else:
                return

            print("\n--- REPUTAÇÃO DO SMARTWATCH ---")
            print(f"Reputação: {self.agent.get_reputation():.2f}")

    async def setup(self):
        print(f"Smartwatch {self.sensor_id} iniciado.")
        self.add_behaviour(self.ReceiveFeedbackBehaviour())
