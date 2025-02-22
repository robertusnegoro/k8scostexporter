import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from prometheus_client import start_http_server, Gauge
from kubernetes import client, config
from decimal import Decimal
from google.cloud import billing_v1
from google.cloud import compute_v1
from cachetools import TTLCache, cached
from datetime import datetime, timezone
import google.auth
from google.api_core import client_options
import sys
import traceback
import signal
from tenacity import retry, stop_after_attempt, wait_exponential

# Setup logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", None)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=LOG_FILE if LOG_FILE else None
)
logger = logging.getLogger(__name__)

# GCP Configuration
PROJECT_ID = os.getenv("GCP_PROJECT_ID")
REGION = os.getenv("GCP_REGION", "us-central1")
ZONE = os.getenv("GCP_ZONE", "us-central1-a")

logger.info(f"Using GCP Project ID: {PROJECT_ID}")

# Cache for pricing data (refresh every 24 hours)
price_cache = TTLCache(maxsize=100, ttl=86400)  # 24 hours
billing_info_cache = TTLCache(maxsize=1, ttl=3600)  # 1 hour

# Load Kubernetes configuration based on environment
KUBE_CONTEXT = os.getenv("KUBE_CONTEXT", None)
IN_CLUSTER = os.getenv("IN_CLUSTER", "false").lower() == "true"

# Add near the top with other environment variables
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
logger.info(f"Using thread pool with {MAX_WORKERS} workers")

if IN_CLUSTER:
    config.load_incluster_config()
    logger.info("Running inside Kubernetes cluster")
elif KUBE_CONTEXT:
    config.load_kube_config(context=KUBE_CONTEXT)
    logger.info(f"Using kube context: {KUBE_CONTEXT}")
else:
    config.load_kube_config()
    logger.info("Using default kube context")

# Initialize GCP clients with project ID and quota project
client_options = client_options.ClientOptions(
    quota_project_id=PROJECT_ID
)

billing_client = billing_v1.CloudBillingClient(
    client_options=client_options
)
catalog_client = billing_v1.CloudCatalogClient(
    client_options=client_options
)
compute_client = compute_v1.MachineTypesClient(
    client_options=client_options
)

# Log the current credentials being used
current_creds, project_id = google.auth.default()
logger.info(f"Currently authenticated as: {current_creds.service_account_email if hasattr(current_creds, 'service_account_email') else 'unknown'}")
logger.info(f"Default project from credentials: {project_id}")
logger.info(f"Using quota project: {PROJECT_ID}")

# Define Prometheus metrics
cpu_usage = Gauge('kube_pod_cpu_usage', 'CPU usage per pod', ['namespace', 'pod'])
mem_usage = Gauge('kube_pod_memory_usage', 'Memory usage per pod', ['namespace', 'pod'])
cpu_cost = Gauge('gcp_pod_cpu_cost_hour', 'Hourly CPU cost per pod', ['namespace', 'pod', 'region', 'machine_type'])
mem_cost = Gauge('gcp_pod_memory_cost_hour', 'Hourly memory cost per pod', ['namespace', 'pod', 'region', 'machine_type'])
total_cost = Gauge('gcp_pod_total_cost_hour', 'Total hourly cost per pod', ['namespace', 'pod', 'region', 'machine_type'])

# Initialize Kubernetes API clients
v1 = client.CoreV1Api()
v1_metrics = client.CustomObjectsApi()

def get_node_machine_type(node_name):
    try:
        node = v1.read_node(node_name)
        return node.metadata.labels.get("node.kubernetes.io/instance-type", "unknown")
    except Exception as e:
        logger.error(f"Failed to fetch machine type for node {node_name}: {e}")
        return "unknown"

@cached(billing_info_cache)
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10),
    reraise=True
)
def get_billing_info():
    """Separate function to handle billing API calls with retry logic"""
    billing_accounts = billing_client.list_billing_accounts()
    billing_account = next(iter(billing_accounts), None)
    if not billing_account:
        raise Exception("No billing accounts found")
    
    services = catalog_client.list_services()
    compute_service = None
    for service in services:
        if "Compute Engine" in service.display_name:
            compute_service = service
            break
    
    if not compute_service:
        raise Exception("Could not find Compute Engine service")
    
    # Cache SKUs for this service
    skus = list(catalog_client.list_skus(parent=compute_service.name))
    return billing_account, compute_service, skus

def get_machine_type_pricing(machine_type="e2-standard-2"):
    cache_key = f"{REGION}-{machine_type}"
    
    # Check cache first
    if cache_key in price_cache:
        logger.debug(f"Cache hit: Found pricing for {machine_type} in region {REGION}")
        return price_cache[cache_key]

    logger.debug(f"Cache miss: Fetching pricing from GCP for {machine_type} in region {REGION}")
    try:
        # Get billing info with retries (now includes SKUs)
        _, _, skus = get_billing_info()
        
        pricing = {
            'cpu_hour': Decimal('0'),
            'memory_gb_hour': Decimal('0')
        }

        machine_family = machine_type.split('-')[0]
        
        for sku in skus:
            if (sku.category.resource_family == "Compute" and 
                REGION in sku.service_regions and 
                machine_family in sku.description.lower()):
                
                if "CPU" in sku.description:
                    unit_price = (sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.units + 
                                sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.nanos / 1e9)
                    pricing['cpu_hour'] = Decimal(str(unit_price))
                    logger.info(f"Found CPU pricing for {machine_type}: ${unit_price}/hour")
                    
                elif "RAM" in sku.description:
                    unit_price = (sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.units + 
                                sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.nanos / 1e9)
                    pricing['memory_gb_hour'] = Decimal(str(unit_price))
                    logger.info(f"Found RAM pricing for {machine_type}: ${unit_price}/GB/hour")

        if pricing['cpu_hour'] == 0 or pricing['memory_gb_hour'] == 0:
            logger.warning(f"Could not find complete pricing for {machine_type}, using defaults")
            pricing = {
                'cpu_hour': Decimal('0.042'),
                'memory_gb_hour': Decimal('0.005')
            }

        # Cache the pricing
        price_cache[cache_key] = pricing
        logger.info(f"Cached pricing for {machine_type} in region {REGION}")
        return pricing

    except Exception as e:
        logger.error(f"Failed to fetch GCP pricing: {e}")
        return {
            'cpu_hour': Decimal('0.042'),
            'memory_gb_hour': Decimal('0.005')
        }

def calculate_costs(cpu_cores, memory_mib, namespace, pod_name, machine_type="e2-standard-2"):
    try:
        pricing = get_machine_type_pricing(machine_type)
        memory_gb = memory_mib / 1024
        cpu_hourly_cost = Decimal(str(cpu_cores)) * pricing["cpu_hour"]
        memory_hourly_cost = Decimal(str(memory_gb)) * pricing["memory_gb_hour"]
        total_hourly_cost = cpu_hourly_cost + memory_hourly_cost
        cpu_cost.labels(namespace, pod_name, REGION, machine_type).set(float(cpu_hourly_cost))
        mem_cost.labels(namespace, pod_name, REGION, machine_type).set(float(memory_hourly_cost))
        total_cost.labels(namespace, pod_name, REGION, machine_type).set(float(total_hourly_cost))
    except Exception as e:
        logger.error(f"Failed to calculate costs: {e}")

def parse_memory_value(mem_value):
    """Convert memory value string to MiB"""
    try:
        if mem_value.endswith('Ki'):
            return int(mem_value.rstrip('Ki')) / 1024  # Convert KiB to MiB
        elif mem_value.endswith('Mi'):
            return int(mem_value.rstrip('Mi'))  # Already in MiB
        elif mem_value.endswith('M'):
            return int(mem_value.rstrip('M'))  # Treat M as MiB
        elif mem_value.endswith('Gi'):
            return int(mem_value.rstrip('Gi')) * 1024  # Convert GiB to MiB
        elif mem_value.endswith('G'):
            return int(mem_value.rstrip('G')) * 1024  # Treat G as GiB
        else:
            # Assume bytes if no unit
            return int(mem_value) / (1024 * 1024)  # Convert bytes to MiB
    except ValueError as e:
        logger.warning(f"Failed to parse memory value {mem_value}: {e}")
        return 0

def fetch_pod_metrics(pod):
    namespace = pod.metadata.namespace
    pod_name = pod.metadata.name
    node_name = pod.spec.node_name
    machine_type = get_node_machine_type(node_name)
    try:
        metrics = v1_metrics.get_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
            name=pod_name
        )
        if "containers" in metrics:
            total_cpu = 0.0
            total_mem = 0.0
            for container in metrics["containers"]:
                cpu_value = container["usage"]["cpu"]
                mem_value = container["usage"]["memory"]
                cpu_millicores = int(cpu_value.rstrip("n")) / 1e6
                total_cpu += cpu_millicores / 1000
                total_mem += parse_memory_value(mem_value)
            cpu_usage.labels(namespace=namespace, pod=pod_name).set(total_cpu)
            mem_usage.labels(namespace=namespace, pod=pod_name).set(total_mem)
            calculate_costs(total_cpu, total_mem, namespace, pod_name, machine_type)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            # Pod no longer exists, remove its metrics
            logger.debug(f"Pod {namespace}/{pod_name} no longer exists, removing metrics")
            try:
                cpu_usage.remove(namespace, pod_name)
                mem_usage.remove(namespace, pod_name)
                cpu_cost.remove(namespace, pod_name, REGION, machine_type)
                mem_cost.remove(namespace, pod_name, REGION, machine_type)
                total_cost.remove(namespace, pod_name, REGION, machine_type)
            except KeyError:
                # Metrics might already be removed
                pass
        else:
            logger.warning(f"Failed to fetch metrics for {namespace}/{pod_name}: {e}")
    except Exception as e:
        logger.warning(f"Failed to fetch metrics for {namespace}/{pod_name}: {e}")

def signal_handler(signum, frame):
    logger.info("Received signal to terminate. Shutting down gracefully...")
    sys.exit(0)

def collect_metrics():
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("Starting metrics collection. Press CTRL+C to exit.")
    while True:
        try:
            pods = v1.list_pod_for_all_namespaces(watch=False).items
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                executor.map(fetch_pod_metrics, pods)
            time.sleep(10)
        except KeyboardInterrupt:
            logger.info("Interrupted by user. Shutting down...")
            break
        except Exception as e:
            logger.error(f"Error in metrics collection: {e}")
            time.sleep(10)  # Wait before retrying

if __name__ == "__main__":
    logger.info("Starting metrics server...")
    start_http_server(8000)
    collect_metrics()
    logger.info("Shutdown complete.")

