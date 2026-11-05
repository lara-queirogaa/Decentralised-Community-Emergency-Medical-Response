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

# até onde uma testemunha consegue ver um acidente
WITNESS_RADIUS = 5.0

# nível de trânsito -> (percentagem de ruas afetadas, multiplicador)
TRAFFIC_LEVELS = {
    1: (0.3, 1.5),
    2: (0.3, 2.0),
    3: (0.5, 2.0),
    4: (0.6, 2.5),
    5: (0.8, 3.0),
}


class Environment:
    """
    Mundo onde os agentes vivem.

    Não é um agente: guarda o mapa, sabe onde está cada agente, calcula
    rotas e provoca eventos (acidentes, estradas cortadas, trânsito).
    As posições são sempre (x, y) e a matriz acede-se como grid[y][x].
    Os agentes devem usar coordenadas inteiras.

    Attributes:
        grid (list[list[int]]): Matriz do mapa, com um código por célula
            (ROAD, BUILDING, HOSPITAL ou ROADBLOCK).
        height (int): Número de linhas do mapa.
        width (int): Número de colunas do mapa.
        agents (dict[str, list]): Agentes registados, por tipo
            ("smartwatch", "witness", "ambulance" e "police").
        accidents (list[dict]): Acidentes que existem no mundo, ou seja, a
            "verdade" da simulação. Cada um tem "position", "real" e "kind".
        g (networkx.Graph): Grafo das estradas. Cada aresta tem um "weight"
            com o custo de a atravessar.
        start_time (float): Instante em que o ambiente foi criado, usado
            por timer().
    """

    def __init__(
        self, width=16, height=16, grid=None, hospitals=None,
        roadblocks=None, traffic_level=0
    ):
        """
        Cria o ambiente, constrói o grafo e aplica o estado inicial do mundo.

        Args:
            width (int): Número de colunas, se nenhuma grelha for dada.
            height (int): Número de linhas, se nenhuma grelha for dada.
            grid (list[list[int]] | None): Mapa já feito. Se for None, gera-se
                uma cidade de exemplo. Quando é dado, width e height são
                ignorados e calculados a partir da grelha.
            hospitals (list[tuple[int, int]] | None): Posições (x, y) a marcar
                como hospital.
            roadblocks (list[tuple[int, int]] | None): Estradas cortadas desde
                o início.
            traffic_level (int): Nível de trânsito inicial, de 0 (nenhum) a 5.
        """
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

        # Acidentes que existem no mundo (a 'verdade' da simulação)
        self.accidents = []

        self.g = nx.Graph()
        self.start_time = time.time()
        self.build_graph()

        # Estado inicial do mundo: estradas cortadas e trânsito
        for position in (roadblocks or []):
            self.add_roadblock(position)

        if traffic_level:
            self.set_traffic(traffic_level)


    # Mapa
    @staticmethod
    def make_city_grid(width, height):
        """
        Gera uma cidade de exemplo, com quarteirões de 2x2 edifícios separados
        por estradas.

        Args:
            width (int): Número de colunas.
            height (int): Número de linhas.

        Returns:
            list[list[int]]: Matriz só com ROAD e BUILDING.
        """
        grid = [[ROAD] * width for _ in range(height)]

        for y in range(height):
            for x in range(width):
                if x % 4 in (1, 2) and y % 4 in (1, 2):
                    grid[y][x] = BUILDING

        return grid

    def build_graph(self):
        """
        Constrói o grafo a partir da grelha.

        Cada célula passável fica ligada às vizinhas (cima, baixo, esquerda e
        direita) com custo 1. Substitui qualquer grafo anterior.
        """
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
        """
        Indica se uma posição está dentro do mapa.

        Args:
            position (tuple[int, int]): Posição (x, y).

        Returns:
            bool: True se a posição estiver dentro dos limites da grelha.
        """
        x, y = position
        return 0 <= x < self.width and 0 <= y < self.height

    def is_road(self, position):
        """
        Indica se uma posição é transitável.

        Args:
            position (tuple[int, int]): Posição (x, y).

        Returns:
            bool: True se estiver dentro do mapa e for estrada ou hospital.
                Edifícios e estradas cortadas devolvem False.
        """
        x, y = position
        return self.in_bounds(position) and self.grid[y][x] in PASSABLE

    def nearest_node(self, position):
        """
        Encontra o ponto de estrada mais próximo de uma posição qualquer.

        Args:
            position (tuple[float, float]): Posição (x, y), mesmo que não seja
                inteira ou não seja estrada.

        Returns:
            tuple[int, int]: A própria posição arredondada, se já for um ponto
                do grafo, ou o ponto de estrada mais próximo.
        """
        node = (round(position[0]), round(position[1]))

        if node in self.g:
            return node

        return min(self.g.nodes, key=lambda n: math.dist(n, position))
    
    # Registo de agentes
    async def add_agent(self, kind, agent):
        """
        Regista um agente no mundo, liga-o ao ambiente e arranca-o.

        Depois desta chamada o agente passa a ter o atributo environment.

        Args:
            kind (str): Tipo do agente: "smartwatch", "witness", "ambulance"
                ou "police".
            agent (spade.agent.Agent): Agente a registar.

        Raises:
            ValueError: Se kind não for um tipo conhecido.
        """
        if kind not in self.agents:
            raise ValueError(f"Tipo de agente desconhecido: {kind}")

        self.agents[kind].append(agent)
        agent.environment = self
        await agent.start()

    def all_agents(self):
        """
        Junta todos os agentes registados numa só lista.

        Returns:
            list: Todos os agentes, de todos os tipos.
        """
        return [a for group in self.agents.values() for a in group]

    def get_nearby(self, kind, position, radius):
        """
        Procura agentes de um tipo dentro de um raio.

        Args:
            kind (str): Tipo de agente a procurar.
            position (tuple[float, float]): Centro da pesquisa.
            radius (float): Distância máxima em linha reta.

        Returns:
            list[tuple]: Pares (agente, distância), do mais perto ao mais
                longe.
        """
        found = []

        for agent in self.agents[kind]:
            distance = math.dist(position, agent.location)
            if distance <= radius:
                found.append((agent, distance))

        found.sort(key=lambda item: item[1])
        return found

    # Rotas
    def shortest_path(self, origin, destination):
        """
        Calcula o caminho mais barato entre dois pontos.

        As posições que não estejam no grafo são trocadas pelo ponto de
        estrada mais próximo.

        Args:
            origin (tuple[float, float]): Ponto de partida.
            destination (tuple[float, float]): Ponto de chegada.

        Returns:
            list[tuple[int, int]] | None: Pontos do caminho, ou None se não
                houver caminho.
        """
        start = self.nearest_node(origin)
        end = self.nearest_node(destination)

        try:
            return nx.shortest_path(self.g, start, end, weight="weight")
        except nx.NetworkXNoPath:
            return None

    def route_cost(self, origin, destination):
        """
        Calcula o custo do melhor caminho entre dois pontos.

        Estradas cortadas e trânsito são tidos em conta, porque alteram o
        grafo e os seus pesos.

        Args:
            origin (tuple[float, float]): Ponto de partida.
            destination (tuple[float, float]): Ponto de chegada.

        Returns:
            float: Soma dos pesos do caminho, ou infinito se não houver
                caminho.
        """
        path = self.shortest_path(origin, destination)

        if path is None:
            return float("inf")

        return nx.path_weight(self.g, path, weight="weight")

    def travel_time(self, origin, destination, speed):
        """
        Calcula o tempo de viagem entre dois pontos.

        Args:
            origin (tuple[float, float]): Ponto de partida.
            destination (tuple[float, float]): Ponto de chegada.
            speed (float): Velocidade em unidades de custo por segundo.

        Returns:
            float: Tempo em segundos, ou infinito se não houver caminho.
        """
        return self.route_cost(origin, destination) / speed

    # Eventos do mundo
    def add_roadblock(self, position):
        """
        Corta uma estrada.

        A célula passa a ROADBLOCK e perde todas as ligações no grafo, por
        isso os caminhos passam a desviar-se dela.

        Args:
            position (tuple[int, int]): Posição (x, y) da estrada a cortar.

        Returns:
            bool: True se foi cortada, False se a posição não for uma estrada.
        """
        x, y = position

        if not self.in_bounds(position) or self.grid[y][x] != ROAD:
            print(f"[ENV] Não é possível cortar {position}.")
            return False

        self.grid[y][x] = ROADBLOCK
        self.g.remove_edges_from(list(self.g.edges((x, y))))
        print(f"[ENV] Estrada cortada em {position}.")
        return True

    def remove_roadblock(self, position):
        """
        Reabre uma estrada que estava cortada.

        Args:
            position (tuple[int, int]): Posição (x, y) da estrada.

        Returns:
            bool: True se foi reaberta, False se não estava cortada.
        """
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
        """
        Define o nível de trânsito.

        Repõe todos os custos a 1 e depois encarece, ao acaso, uma percentagem
        das ruas. Por isso, chamar várias vezes não acumula trânsito.

        Args:
            level (int): 0 para nenhum trânsito, ou 1 a 5 para trânsito cada
                vez mais pesado.
        """
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

    # Acidentes: dois tipos
    def is_real_accident(self, position, radius=2.0):
        """
        Indica se existe um acidente real perto de uma posição.

        É consultado pelas testemunhas para confirmar ou negar o alerta de um
        smartwatch.

        Args:
            position (tuple[float, float]): Posição a verificar.
            radius (float): Distância máxima ao acidente.

        Returns:
            bool: True se houver um acidente real dentro do raio.
        """
        return any(
            accident["real"]
            and math.dist(accident["position"], position) <= radius
            for accident in self.accidents
        )

    async def trigger_smartwatch_accident(
        self, smartwatch, vitals=None, position=None, real=True
    ):
        """
        Provoca um acidente do tipo 1: o smartwatch da vítima deteta-o.

        O alerta precisa de confirmação de uma testemunha.

        Args:
            smartwatch (SmartwatchAgent): Smartwatch da vítima.
            vitals (dict | None): Sinais vitais da vítima, com "heart_rate" e
                "breathing".
            position (tuple[int, int] | None): Onde está a vítima. Se for None,
                mantém-se a posição atual do smartwatch.
            real (bool): False simula um falso alarme, em que a testemunha vai
                negar.
        """
        if position is not None:
            smartwatch.location = self.nearest_node(position)

        if vitals is not None:
            smartwatch.vitals = vitals

        location = tuple(smartwatch.location)
        self.accidents.append(
            {"position": location, "real": real, "kind": "smartwatch"}
        )

        kind = "real" if real else "FALSO ALARME"
        print(f"\n[ENV] Smartwatch deteta acidente em {location} ({kind}).")

        smartwatch.add_behaviour(smartwatch.DetectIncidentBehaviour())

    async def trigger_witness_accident(
        self, position, vitals=None, radius=WITNESS_RADIUS
    ):
        """
        Provoca um acidente do tipo 2: é uma testemunha que o vê e reporta.

        O smartwatch não intervém. Se não houver nenhuma testemunha dentro do
        alcance, ninguém dá o alerta.

        Args:
            position (tuple[int, int]): Onde acontece o acidente.
            vitals (dict | None): Sinais vitais que a testemunha observa.
            radius (float): Alcance de visão das testemunhas.

        Returns:
            WitnessAgent | None: A testemunha que reportou, ou None se
                ninguém viu o acidente.
        """
        position = self.nearest_node(position)
        self.accidents.append(
            {"position": position, "real": True, "kind": "witness"}
        )

        nearby = self.get_nearby("witness", position, radius)

        if not nearby:
            print(
                f"\n[ENV] Acidente em {position}, mas nenhuma testemunha "
                f"o viu. Ninguém deu o alerta."
            )
            return None

        witness, distance = nearby[0]
        print(
            f"\n[ENV] Acidente em {position}. "
            f"Visto pela testemunha {witness.witness_id} "
            f"(distância: {distance:.1f})."
        )

        witness.accident_location = position
        witness.vitals = vitals
        witness.add_behaviour(witness.DetectIncidentBehaviour())
        return witness

    async def random_accident(self, radius=WITNESS_RADIUS):
        """
        Provoca um acidente aleatório, visto só por testemunhas.

        Escolhe uma testemunha ao acaso e um ponto de estrada no seu alcance,
        para haver sempre alguém que o veja. Com 30% de probabilidade a vítima
        está em paragem cardiorrespiratória.

        Args:
            radius (float): Alcance de visão das testemunhas.

        Returns:
            WitnessAgent | None: A testemunha que reportou, ou None se não
                houver testemunhas registadas.
        """
        witnesses = self.agents["witness"]
        if not witnesses:
            print("[ENV] Não há testemunhas registadas.")
            return None

        witness = random.choice(witnesses)
        candidates = [
            node for node in self.g.nodes
            if self.is_road(node)
            and math.dist(node, witness.location) <= radius
        ]
        position = random.choice(candidates)

        if random.random() < 0.3:
            vitals = {"heart_rate": 0, "breathing": False}
        else:
            vitals = {
                "heart_rate": random.randint(50, 130),
                "breathing": True
            }

        return await self.trigger_witness_accident(
            position, vitals, radius
        )

    # Tempo e visualização
    def timer(self):
        """
        Mede o tempo de simulação.

        Returns:
            int: Segundos desde a criação do ambiente.
        """
        return int(time.time() - self.start_time)

    def render(self):
        """
        Desenha o mapa em texto.

        Símbolos: "." estrada, "#" edifício, "H" hospital, "X" estrada cortada,
        "W" testemunha, "P" polícia, "A" ambulância, "S" smartwatch, "!" acidente
        real e "~" falso alarme.

        Returns:
            str: O mapa, uma linha por fila da grelha.
        """
        symbols = {
            ROAD: ".", BUILDING: "#", HOSPITAL: "H", ROADBLOCK: "X"
        }
        rows = [[symbols.get(c, "?") for c in row] for row in self.grid]

        for accident in self.accidents:
            x = round(accident["position"][0])
            y = round(accident["position"][1])
            if 0 <= x < self.width and 0 <= y < self.height:
                rows[y][x] = "!" if accident["real"] else "~"

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
        """
        Reúne os dados necessários para uma interface gráfica.

        Returns:
            tuple: (grid, agents, traffic_edges), onde traffic_edges são as
                arestas com custo superior a 1.
        """
        traffic_edges = [
            edge for edge in self.g.edges
            if self.g.edges[edge].get("weight", 1) > 1
        ]
        return self.grid, self.agents, traffic_edges