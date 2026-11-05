import math
import random
import time

import networkx as nx

# Códigos das células da matriz (grid[y][x])
ROAD = 0
BUILDING = 1
HOSPITAL = 2
ROADBLOCK = 9

PASSABLE = (ROAD, HOSPITAL)

# nível de trânsito -> (percentagem de ruas afetadas, multiplicador)
TRAFFIC_LEVELS = {
    1: (0.3, 1.5),
    2: (0.3, 2.0),
    3: (0.5, 2.0),
    4: (0.6, 2.5),
    5: (0.8, 3.0),
}


class Environment:
    """O mundo onde os agentes vivem.

    Não é um agente: guarda o mapa, sabe onde está cada agente, calcula
    rotas e provoca eventos (acidentes, estradas cortadas, trânsito).

    Convenção: posições são sempre (x, y) e a matriz acede-se grid[y][x].
    Usa coordenadas inteiras para os agentes.
    """

    def __init__(self, width=16, height=16, grid=None, hospitals=None):
        if grid is None:
            grid = self.make_city_grid(width, height)

        self.grid = grid
        self.height = len(grid)
        self.width = len(grid[0])

        self.agents = {
            "smartwatch": [],
            "witness": [],
            "ambulance": [],
            "police": []
        }

        for x, y in (hospitals or []):
            self.grid[y][x] = HOSPITAL

        self.g = nx.Graph()
        self.start_time = time.time()
        self.build_graph()

    # ------------------------------------------------------------------
    # Mapa
    # ------------------------------------------------------------------

    @staticmethod
    def make_city_grid(width, height):
        """Quarteirões de 2x2 edifícios separados por estradas."""
        grid = [[ROAD] * width for _ in range(height)]

        for y in range(height):
            for x in range(width):
                if x % 4 in (1, 2) and y % 4 in (1, 2):
                    grid[y][x] = BUILDING

        return grid

    def build_graph(self):
        self.g.clear()

        for y in range(self.height):
            for x in range(self.width):
                if self.grid[y][x] in PASSABLE:
                    self.g.add_node((x, y))

        # só direita e baixo, para não duplicar arestas
        for x, y in list(self.g.nodes):
            for dx, dy in ((1, 0), (0, 1)):
                neighbour = (x + dx, y + dy)
                if neighbour in self.g:
                    self.g.add_edge((x, y), neighbour, weight=1)

    def in_bounds(self, position):
        x, y = position
        return 0 <= x < self.width and 0 <= y < self.height

    def is_road(self, position):
        x, y = position
        return self.in_bounds(position) and self.grid[y][x] in PASSABLE

    def nearest_node(self, position):
        """Ponto de estrada mais próximo de uma posição qualquer."""
        node = (round(position[0]), round(position[1]))

        if node in self.g:
            return node

        return min(self.g.nodes, key=lambda n: math.dist(n, position))

    # Registo de agentes
    async def add_agent(self, kind, agent):
        """Regista o agente no mundo, liga-o ao ambiente e arranca-o.
        kind: 'smartwatch' | 'witness' | 'ambulance' | 'police'"""
        if kind not in self.agents:
            raise ValueError(f"Tipo de agente desconhecido: {kind}")

        self.agents[kind].append(agent)
        agent.environment = self
        await agent.start()

    def all_agents(self):
        return [a for group in self.agents.values() for a in group]

    def get_nearby(self, kind, position, radius):
        """Agentes de um tipo dentro de um raio, do mais perto ao mais
        longe. Devolve lista de (agente, distância)."""
        found = []

        for agent in self.agents[kind]:
            distance = math.dist(position, agent.location)
            if distance <= radius:
                found.append((agent, distance))

        found.sort(key=lambda item: item[1])
        return found

    
    # Rotas
    def shortest_path(self, origin, destination):
        start = self.nearest_node(origin)
        end = self.nearest_node(destination)

        try:
            return nx.shortest_path(self.g, start, end, weight="weight")
        except nx.NetworkXNoPath:
            return None

    def route_cost(self, origin, destination):
        """Custo do melhor caminho (inf se não houver caminho)."""
        path = self.shortest_path(origin, destination)

        if path is None:
            return float("inf")

        return nx.path_weight(self.g, path, weight="weight")

    def travel_time(self, origin, destination, speed):
        """Segundos de viagem a 'speed' unidades/segundo
        (inf se não houver caminho)."""
        return self.route_cost(origin, destination) / speed

    # Eventos do mundo
    def add_roadblock(self, position):
        """Corta uma estrada. position = (x, y)."""
        x, y = position

        if not self.in_bounds(position) or self.grid[y][x] != ROAD:
            print(f"[ENV] Não é possível cortar {position}.")
            return False

        self.grid[y][x] = ROADBLOCK
        self.g.remove_edges_from(list(self.g.edges((x, y))))
        print(f"[ENV] Estrada cortada em {position}.")
        return True

    def remove_roadblock(self, position):
        x, y = position

        if not self.in_bounds(position) or self.grid[y][x] != ROADBLOCK:
            return False

        self.grid[y][x] = ROAD

        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbour = (x + dx, y + dy)
            if self.is_road(neighbour):
                self.g.add_edge((x, y), neighbour, weight=1)

        print(f"[ENV] Estrada reaberta em {position}.")
        return True

    def set_traffic(self, level):
        """0 = sem trânsito; 1 a 5 = cada vez mais trânsito."""
        for edge in self.g.edges:
            self.g.edges[edge]["weight"] = 1

        if level == 0:
            print("[ENV] Trânsito normal.")
            return

        if level not in TRAFFIC_LEVELS:
            print("[ENV] Nível de trânsito inválido.")
            return

        percentage, multiplier = TRAFFIC_LEVELS[level]
        edges = list(self.g.edges)
        chosen = random.sample(edges, int(len(edges) * percentage))

        for edge in chosen:
            self.g.edges[edge]["weight"] = multiplier

        print(f"[ENV] Trânsito nível {level}.")

    async def trigger_accident(self, reporter, position, vitals=None):
        """Provoca um acidente. O 'reporter' (smartwatch ou witness)
        passa a estar no local e deteta-o."""
        position = self.nearest_node(position)

        reporter.location = position
        if vitals is not None:
            reporter.vitals = vitals

        print(f"\n[ENV] Acidente em {position}. Vitais: {vitals}")
        reporter.add_behaviour(reporter.DetectIncidentBehaviour())

    async def random_accident(self):
        """Acidente num ponto aleatório, num smartwatch aleatório."""
        watches = self.agents["smartwatch"]
        if not watches:
            return None

        watch = random.choice(watches)
        position = random.choice(list(self.g.nodes))

        if random.random() < 0.3:
            vitals = {"heart_rate": 0, "breathing": False}
        else:
            vitals = {
                "heart_rate": random.randint(50, 130),
                "breathing": True
            }

        await self.trigger_accident(watch, position, vitals)
        return position

    # Tempo e visualização
    def timer(self):
        """Segundos desde o início da simulação."""
        return int(time.time() - self.start_time)

    def render(self):
        """Mapa em texto: . estrada, # edifício, H hospital, X estrada
        cortada, W testemunha, P polícia, A ambulância, S smartwatch."""
        symbols = {
            ROAD: ".", BUILDING: "#", HOSPITAL: "H", ROADBLOCK: "X"
        }
        rows = [[symbols.get(c, "?") for c in row] for row in self.grid]

        for kind, symbol in (
            ("witness", "W"), ("police", "P"),
            ("ambulance", "A"), ("smartwatch", "S")
        ):
            for agent in self.agents[kind]:
                x = round(agent.location[0])
                y = round(agent.location[1])
                if 0 <= x < self.width and 0 <= y < self.height:
                    rows[y][x] = symbol

        return "\n".join(" ".join(row) for row in rows)

    def update_display(self):
        """Dados para uma eventual interface gráfica."""
        traffic_edges = [
            edge for edge in self.g.edges
            if self.g.edges[edge].get("weight", 1) > 1
        ]
        return self.grid, self.agents, traffic_edges