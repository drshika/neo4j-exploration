import os
import time
import requests
from colorama import init, Fore, Style
from neo4j import GraphDatabase

init()

class KnowledgeGraphClient:
    def __init__(self):
        self.neo4j_uri = os.getenv("NEO4J_URI", "neo4j://localhost:7687")
        self.server_url = os.getenv("SERVER_URL", "http://kg-server:8080")

    def load_csv_data(self, file_path):
        try:
            import csv
            data = []
            with open(file_path, 'r') as file:
                reader = csv.DictReader(file, delimiter=',')
                for row in reader:
                    data.append(row)
            self.log_status(f"Successfully loaded data from {file_path}")
            return data
        except Exception as e:
            self.log_error(f"Failed to load {file_path}: {str(e)}")
            return None

    def validate_role_data(self, role):
        required_fields = [':START_ID', ':END_ID', 'relation', 'weight', 'method', ':TYPE']
        missing_fields = [field for field in required_fields if field not in role]
        
        if missing_fields:
            self.log_warning(f"Missing required fields in role: {missing_fields}")
            return False
        return True
    
    def create_nodes(self, session, entities):
        try:
            session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE")
            
            batch_size = 1000
            total = len(entities)
            created = 0
            
            label_groups = {}
            for entity in entities:
                labels = entity[':LABEL'].replace(' ', '_').split(';')
                combined_label = ':'.join(labels)
                if combined_label not in label_groups:
                    label_groups[combined_label] = []
                label_groups[combined_label].append(entity)
            
            for labels, group in label_groups.items():
                for i in range(0, len(group), batch_size):
                    batch = group[i:i + batch_size]
                    query = f"""
                    UNWIND $batch AS entity
                    CREATE (n:{labels} {{
                        entity_id: entity.`entity:ID`,
                        name: entity.name,
                        type: entity.type,
                        frequency: toInteger(entity.frequency)
                    }})
                    """
                    session.run(query, {"batch": batch})
                    created += len(batch)
                    if created % 100 == 0:
                        self.log_status(f"Created {created}/{total} nodes")
                
            self.log_status("Successfully created all nodes")
        except Exception as e:
            self.log_error(f"Failed to create nodes: {str(e)}")

    def sanitize_relationship_type(self,rel_type):
        sanitized = ''.join(c for c in rel_type if c.isalnum() or c == '_')
        if sanitized[0].isdigit():
            sanitized = 'REL_' + sanitized
        return sanitized.upper()

    def create_relationships(self, session, roles):
        try:
            self.log_status(f"Starting relationship creation with {len(roles) if roles else 0} roles")
            if not roles:
                self.log_error("No roles data provided")
                return

            if roles and len(roles) > 0:
                self.log_status("First role data sample:")
                self.log_status(str(roles[0]))
                self.log_status("Validating role data structure...")
                if not self.validate_role_data(roles[0]):
                    raise ValueError("Invalid role data structure")

            batch_size = 1000
            total = len(roles)
            created = 0

            for i in range(0, total, batch_size):
                batch = roles[i:i + batch_size]

                type_groups = {}
                for role in batch:
                    rel_type = self.sanitize_relationship_type(role[':TYPE'])
                    if rel_type not in type_groups:
                        type_groups[rel_type] = []
                    type_groups[rel_type].append(role)

                for rel_type, roles_group in type_groups.items():
                    query = f"""
                    UNWIND $batch AS role
                    MATCH (source {{entity_id: role.`:START_ID`}}), (target {{entity_id: role.`:END_ID`}})
                    MERGE (source)-[r:{rel_type} {{
                        relation: role.relation,
                        weight: toFloat(role.weight),
                        method: role.method
                    }}]->(target)
                    RETURN source.entity_id, type(r), target.entity_id
                    """

                    tx = session.begin_transaction()
                    try:
                        tx.run(query, {'batch': roles_group})
                        tx.commit()
                    except Exception as tx_error:
                        tx.rollback()
                        raise tx_error
                
                verify_query = """
                UNWIND $types as type
                MATCH ()-[r]-()
                WHERE type(r) = type
                WITH type, count(r) as count
                RETURN type, count
                """

                types = [role[':TYPE'] for role in batch]

                verify_result = session.run(verify_query, {
                    'types': types
                }).data()

                if verify_result:
                    self.log_status(f"Verification results: {verify_result}")
                    created += len(batch)
                    if created % 1000 == 0:
                        self.log_status(f"Created {created}/{total} relationships")
                else:
                    self.log_warning(f"Batch verification failed for relationships {i} to {i+len(batch)}")
                    # self.log_warning(f"Types being verified: {types}")
                        
            self.log_status("Successfully created all relationships")
        except Exception as e:
            self.log_error(f"Failed to create relationships: {str(e)}")
            raise e
            
    def build_knowledge_graph(self, entities_file, roles_file):
        try:
            entities = self.load_csv_data(entities_file)
            roles = self.load_csv_data(roles_file)
            
            if not entities or not roles:
                return False
                
            driver = GraphDatabase.driver(self.neo4j_uri)
            
            with driver.session() as session:
                self.log_status("Clearing existing graph data...")
                session.run("MATCH (n) DETACH DELETE n")
                
                self.log_status("Creating nodes...")
                self.create_nodes(session, entities)
                node_count = session.run("MATCH (n) RETURN count(n) as count").single()
                self.log_status(f"Verified node count: {node_count['count']}")
                
                self.log_status("Creating relationships...")
                self.create_relationships(session, roles)

                with driver.session() as count_session:
                    rel_count = count_session.run("MATCH ()-[r]->() RETURN count(r) as count").single()
                    self.log_status(f"Verified relationship count: {rel_count['count']}")
                    
                    stats = count_session.run("""
                        MATCH (n)
                        WITH count(DISTINCT n) as nodes
                        MATCH ()-[r]->()
                        RETURN nodes, count(DISTINCT r) as relationships
                    """).single()
                
                if not stats:
                    raise Exception("Failed to retrieve graph statistics - graph may be empty")
                
                self.log_status(f"Knowledge graph built successfully:")
                self.log_status(f"- Nodes: {stats['nodes']}")
                self.log_status(f"- Relationships: {stats['relationships']}")
                
            driver.close()
            return True
            
        except Exception as e:
            self.log_error(f"Failed to build knowledge graph: {str(e)}")
            return False

    def log_status(self, message):
        print(f"{Fore.GREEN}[✓]{Style.RESET_ALL} {message}")

    def log_warning(self, message):
        print(f"{Fore.YELLOW}[!]{Style.RESET_ALL} {message}")

    def log_error(self, message):
        print(f"{Fore.RED}[✗]{Style.RESET_ALL} {message}")

    def test_connection(self):
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                driver = GraphDatabase.driver(self.neo4j_uri)
                with driver.session(database="system") as session:
                    result = session.run("SHOW DATABASE neo4j")
                    record = result.single()
                    if record and record["currentStatus"] == "online":
                        self.log_status("Successfully connected to Neo4j")
                    else:
                        raise Exception("Database is not online")
                driver.close()
                
                return True
            except Exception as e:
                if attempt < max_retries - 1:
                    self.log_warning(f"Connection attempt {attempt + 1} failed, retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    self.log_error(f"Connection failed after {max_retries} attempts: {str(e)}")
                    return False

if __name__ == "__main__":
    client = KnowledgeGraphClient()
    base_path = os.path.dirname(os.path.abspath(__file__))
    entities_path = os.path.join(base_path, '..', "/Users/lmarini/data/combini/Entities.csv")
    roles_path = os.path.join(base_path, '..', "/Users/lmarini/data/combini/Roles.csv")
    if client.test_connection():
        client.build_knowledge_graph(
            entities_path,
            roles_path,
        )