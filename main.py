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
from prometheus_api_client import PrometheusConnect

# =============================================================================
# Environment Configuration
# =============================================================================
class Config:
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE = os.getenv("LOG_FILE", None)

    # GCP Settings
    PROJECT_ID = os.getenv("GCP_PROJECT_ID")
    REGION = os.getenv("GCP_REGION", "us-central1")
    ZONE = os.getenv("GCP_ZONE", "us-central1-a")

    # Kubernetes Settings
    KUBE_CONTEXT = os.getenv("KUBE_CONTEXT", None)
    IN_CLUSTER = os.getenv("IN_CLUSTER", "false").lower() == "true"
    MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))

    # Prometheus Settings
    PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", None)
    PRICE_UPDATE_INTERVAL = int(os.getenv("PRICE_UPDATE_INTERVAL", "30"))  # days

    # Collection Settings
    COLLECTION_INTERVAL = int(os.getenv("COLLECTION_INTERVAL", "300"))  # seconds, defaults to 5 minutes

    # Metric Names
    SKU_PRICE_METRIC = 'k8s_cost_gcp_sku_price'

    # Cache Settings
    BILLING_CACHE_TTL = 3600  # 1 hour
    PRICE_CACHE_TTL = 86400  # 24 hours

# Setup logging
logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    filename=Config.LOG_FILE if Config.LOG_FILE else None
)
logger = logging.getLogger(__name__)

logger.info(f"Using GCP Project ID: {Config.PROJECT_ID}")
logger.info(f"Using thread pool with {Config.MAX_WORKERS} workers")

# Initialize caches
billing_info_cache = TTLCache(maxsize=1, ttl=Config.BILLING_CACHE_TTL)
# Initialize price cache only if needed (no Prometheus)
price_cache = TTLCache(maxsize=100, ttl=Config.PRICE_CACHE_TTL) if not Config.PROMETHEUS_URL else None

# Load Kubernetes configuration
try:
    if Config.IN_CLUSTER:
        config.load_incluster_config()
        logger.info("Running inside Kubernetes cluster")
    elif Config.KUBE_CONTEXT:
        config.load_kube_config(context=Config.KUBE_CONTEXT)
        logger.info(f"Using kube context: {Config.KUBE_CONTEXT}")
    else:
        # Try loading default config, fall back to local development mode if it fails
        try:
            config.load_kube_config()
            logger.info("Using default kube context")
        except config.config_exception.ConfigException as e:
            logger.warning(f"Failed to load kubernetes config: {e}")
            logger.warning("Running in local development mode - some features may be limited")
            # Initialize dummy k8s clients that won't be used
            v1 = None
            v1_metrics = None
            # Skip the rest of the kubernetes initialization
            raise Exception("Skip k8s initialization")

    # Initialize Kubernetes API clients
    v1 = client.CoreV1Api()
    v1_metrics = client.CustomObjectsApi()

except Exception as e:
    if str(e) != "Skip k8s initialization":
        logger.error(f"Failed to initialize kubernetes clients: {e}")
        logger.warning("Running in local development mode - some features may be limited")
        v1 = None
        v1_metrics = None

# Initialize GCP clients
client_options = client_options.ClientOptions(
    quota_project_id=Config.PROJECT_ID
)

billing_client = billing_v1.CloudBillingClient(client_options=client_options)
catalog_client = billing_v1.CloudCatalogClient(client_options=client_options)
compute_client = compute_v1.MachineTypesClient(client_options=client_options)

# Initialize Prometheus client only if URL is provided
prom = None
if Config.PROMETHEUS_URL:
    prom = PrometheusConnect(url=Config.PROMETHEUS_URL, disable_ssl=True)
    logger.info(f"Connected to Prometheus at {Config.PROMETHEUS_URL}")
else:
    logger.info("No Prometheus URL provided, using local cache for pricing data")

# Log authentication details
current_creds, project_id = google.auth.default()
logger.info(f"Currently authenticated as: {current_creds.service_account_email if hasattr(current_creds, 'service_account_email') else 'unknown'}")
logger.info(f"Default project from credentials: {project_id}")
logger.info(f"Using quota project: {Config.PROJECT_ID}")

# Define Prometheus metrics
cpu_usage = Gauge('k8s_cost_pod_cpu_usage', 'CPU usage per pod', ['namespace', 'pod'])
mem_usage = Gauge('k8s_cost_pod_memory_usage', 'Memory usage per pod', ['namespace', 'pod'])
cpu_cost = Gauge('k8s_cost_pod_cpu_cost_hour', 'Hourly CPU cost per pod', ['namespace', 'pod', 'region', 'machine_type'])
mem_cost = Gauge('k8s_cost_pod_memory_cost_hour', 'Hourly memory cost per pod', ['namespace', 'pod', 'region', 'machine_type'])
total_cost = Gauge('k8s_cost_pod_total_cost_hour', 'Total hourly cost per pod', ['namespace', 'pod', 'region', 'machine_type'])

# Update SKU pricing gauge
sku_price = Gauge('k8s_cost_gcp_sku_price', 'GCP SKU pricing information', 
                 ['machine_type', 'region', 'resource_type'])

# Update collector metrics
collector_pods_processed = Gauge('k8s_cost_collector_pods_processed_total', 'Number of pods processed in current cycle')
collector_pods_failed = Gauge('k8s_cost_collector_pods_failed_total', 'Number of pods that failed processing in current cycle')
collector_processing_seconds = Gauge('k8s_cost_collector_processing_seconds', 'Time taken to process all pods')

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

def update_pricing_metrics():
    """Update either Prometheus metrics or local cache with current GCP pricing"""
    logger.info("Updating pricing data...")
    try:
        machine_types = ["e2-standard-2", "e2-standard-4", "e2-standard-8"]
        
        for machine_type in machine_types:
            try:
                _, _, skus = get_billing_info()
                machine_family = machine_type.split('-')[0]
                
                for sku in skus:
                    if (sku.category.resource_family == "Compute" and 
                        Config.REGION in sku.service_regions and 
                        machine_family in sku.description.lower()):
                        
                        if "CPU" in sku.description:
                            unit_price = (sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.units + 
                                        sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.nanos / 1e9)
                            
                            if Config.PROMETHEUS_URL:
                                # Update the Gauge metric
                                sku_price.labels(
                                    machine_type=machine_type,
                                    region=Config.REGION,
                                    resource_type="cpu_hour"
                                ).set(unit_price)
                            else:
                                # Update local cache
                                cache_key = f"{Config.REGION}-{machine_type}-cpu"
                                price_cache[cache_key] = unit_price
                                
                            logger.info(f"Updated CPU pricing for {machine_type}: ${unit_price}/hour")
                            
                        elif "RAM" in sku.description:
                            unit_price = (sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.units + 
                                        sku.pricing_info[0].pricing_expression.tiered_rates[0].unit_price.nanos / 1e9)
                            
                            if Config.PROMETHEUS_URL:
                                # Update the Gauge metric
                                sku_price.labels(
                                    machine_type=machine_type,
                                    region=Config.REGION,
                                    resource_type="memory_gb_hour"
                                ).set(unit_price)
                            else:
                                # Update local cache
                                cache_key = f"{Config.REGION}-{machine_type}-memory"
                                price_cache[cache_key] = unit_price
                                
                            logger.info(f"Updated RAM pricing for {machine_type}: ${unit_price}/GB/hour")

            except Exception as e:
                logger.error(f"Failed to update pricing for {machine_type}: {e}")
                # Set default prices
                if Config.PROMETHEUS_URL:
                    sku_price.labels(
                        machine_type=machine_type,
                        region=Config.REGION,
                        resource_type="cpu_hour"
                    ).set(0.042)
                    sku_price.labels(
                        machine_type=machine_type,
                        region=Config.REGION,
                        resource_type="memory_gb_hour"
                    ).set(0.005)
                else:
                    price_cache[f"{Config.REGION}-{machine_type}-cpu"] = 0.042
                    price_cache[f"{Config.REGION}-{machine_type}-memory"] = 0.005

    except Exception as e:
        logger.error(f"Failed to update pricing data: {e}")

def get_machine_type_pricing(machine_type="e2-standard-2"):
    """Get pricing from either Prometheus metrics or local cache"""
    try:
        if Config.PROMETHEUS_URL:
            # Get from Prometheus metrics
            cpu_price = sku_price.labels(
                machine_type=machine_type,
                region=Config.REGION,
                resource_type="cpu_hour"
            )._value.get()
            
            memory_price = sku_price.labels(
                machine_type=machine_type,
                region=Config.REGION,
                resource_type="memory_gb_hour"
            )._value.get()
        else:
            # Get from local cache
            cpu_price = price_cache.get(f"{Config.REGION}-{machine_type}-cpu")
            memory_price = price_cache.get(f"{Config.REGION}-{machine_type}-memory")
        
        return {
            'cpu_hour': Decimal(str(cpu_price if cpu_price is not None else 0.042)),
            'memory_gb_hour': Decimal(str(memory_price if memory_price is not None else 0.005))
        }
    except Exception as e:
        logger.error(f"Failed to get pricing: {e}")
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
        cpu_cost.labels(namespace, pod_name, Config.REGION, machine_type).set(float(cpu_hourly_cost))
        mem_cost.labels(namespace, pod_name, Config.REGION, machine_type).set(float(memory_hourly_cost))
        total_cost.labels(namespace, pod_name, Config.REGION, machine_type).set(float(total_hourly_cost))
    except Exception as e:
        logger.error(f"Failed to calculate costs: {e}")

def parse_memory_value(mem_value):
    """Convert memory value string to MiB"""
    try:
        # Remove any whitespace
        mem_value = mem_value.strip()
        
        # Handle CPU-style values (ending in 'u' or 'n')
        if mem_value.endswith('u'):
            return int(mem_value.rstrip('u')) / (1024 * 1024)  # Convert to MiB
        elif mem_value.endswith('n'):
            return int(mem_value.rstrip('n')) / (1024 * 1024 * 1e9)  # Convert nano to MiB
        
        # Handle memory units
        elif mem_value.endswith('Ki'):
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
                cpu_cost.remove(namespace, pod_name, Config.REGION, machine_type)
                mem_cost.remove(namespace, pod_name, Config.REGION, machine_type)
                total_cost.remove(namespace, pod_name, Config.REGION, machine_type)
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
    
    logger.info(f"Starting metrics collection with {Config.COLLECTION_INTERVAL} seconds interval")
    
    # Initial price update
    update_pricing_metrics()
    last_price_update = datetime.now(timezone.utc)
    
    while True:
        try:
            collection_start = time.time()
            
            # Check if it's time to update prices
            now = datetime.now(timezone.utc)
            if (now - last_price_update).days >= Config.PRICE_UPDATE_INTERVAL:
                update_pricing_metrics()
                last_price_update = now
            
            # Only collect pod metrics if kubernetes is configured
            if v1 and v1_metrics:
                pods = v1.list_pod_for_all_namespaces(watch=False).items
                total_pods = len(pods)
                processed_pods = 0
                failed_pods = 0
                start_time = time.time()

                logger.info(f"Starting metrics collection for {total_pods} pods...")

                def process_pod_with_progress(pod):
                    nonlocal processed_pods, failed_pods
                    try:
                        fetch_pod_metrics(pod)
                        processed_pods += 1
                        if processed_pods % 100 == 0:  # Log every 100 pods
                            elapsed = time.time() - start_time
                            rate = processed_pods / elapsed
                            remaining = (total_pods - processed_pods) / rate if rate > 0 else 0
                            logger.info(
                                f"Progress: {processed_pods}/{total_pods} pods processed "
                                f"({(processed_pods/total_pods)*100:.1f}%) - "
                                f"Rate: {rate:.1f} pods/sec - "
                                f"Est. remaining: {remaining:.1f} seconds"
                            )
                    except Exception as e:
                        failed_pods += 1
                        logger.error(f"Failed to process pod {pod.metadata.namespace}/{pod.metadata.name}: {e}")

                with ThreadPoolExecutor(max_workers=Config.MAX_WORKERS) as executor:
                    executor.map(process_pod_with_progress, pods)

                # Final statistics
                total_time = time.time() - start_time
                logger.info(
                    f"Metrics collection completed in {total_time:.1f} seconds:\n"
                    f"- Total pods: {total_pods}\n"
                    f"- Successfully processed: {processed_pods}\n"
                    f"- Failed: {failed_pods}\n"
                    f"- Average rate: {processed_pods/total_time:.1f} pods/sec"
                )

                # Update collector metrics
                collector_pods_processed.set(processed_pods)
                collector_pods_failed.set(failed_pods)
                collector_processing_seconds.set(total_time)

            else:
                logger.debug("Skipping pod metrics collection - kubernetes not configured")
            
            # Calculate sleep time based on collection duration
            collection_duration = time.time() - collection_start
            sleep_time = max(0, Config.COLLECTION_INTERVAL - collection_duration)
            
            if sleep_time > 0:
                logger.debug(f"Waiting {sleep_time:.1f} seconds until next collection cycle...")
                time.sleep(sleep_time)
            else:
                logger.warning(
                    f"Collection took longer than interval "
                    f"({collection_duration:.1f} > {Config.COLLECTION_INTERVAL} seconds). "
                    "Starting next cycle immediately."
                )
                
        except KeyboardInterrupt:
            logger.info("Interrupted by user. Shutting down...")
            break
        except Exception as e:
            logger.error(f"Error in metrics collection: {e}")
            time.sleep(Config.COLLECTION_INTERVAL)  # On error, wait full interval

if __name__ == "__main__":
    logger.info("Starting metrics server...")
    start_http_server(8000)
    collect_metrics()
    logger.info("Shutdown complete.")
