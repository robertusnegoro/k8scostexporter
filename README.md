# Kubernetes Cost Exporter (k8scostexporter)

A Prometheus exporter that calculates and exposes cost metrics for Kubernetes resources (pods, storage) running on Google Cloud Platform (GCP).

## Features
- Real-time pod resource usage monitoring
- Multi-cloud provider support (currently GCP, others coming soon)
- Cost calculation for pods and storage (storage coming soon)
- Flexible pricing storage options:
  - Prometheus storage (recommended for production)
  - Local cache fallback (useful for development)
- Prometheus metrics export
- Configurable caching to minimize API calls
- Support for both in-cluster and external deployment


## Prerequisites
- Python 3.8+
- Access to a Kubernetes cluster
- GCP Service Account with the following permissions:
  - `billing.accounts.get`
  - `billing.accounts.list`
  - `cloudbilling.services.get`
  - `cloudbilling.services.list`
- kubectl configured with cluster access
- GCP credentials configured
- (Optional) Prometheus server for pricing storage

## Installation

1. Clone the repository
```bash
git clone https://github.com/yourusername/k8scostexporter.git
cd k8scostexporter
```

2. Create and activate virtual environment
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies
```bash
pip install -r requirements.txt
```

## Configuration

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| GCP_PROJECT_ID | Yes | None | Your GCP project ID |
| GCP_REGION | No | us-central1 | GCP region for pricing |
| GCP_ZONE | No | us-central1-a | GCP zone for instance lookup |
| KUBE_CONTEXT | No | None | Kubernetes context to use |
| IN_CLUSTER | No | false | Set to "true" when running inside Kubernetes |
| LOG_LEVEL | No | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| LOG_FILE | No | None | Log file path (logs to stdout if not set) |
| MAX_WORKERS | No | 8 | Number of threads for parallel processing |
| PROMETHEUS_URL | No | None | Prometheus server URL for pricing storage |
| PRICE_UPDATE_INTERVAL | No | 30 | Days between price updates |

## Storage Modes

### Prometheus Storage Mode (Recommended)
When `PROMETHEUS_URL` is set:
- SKU pricing data is stored as Prometheus metrics
- Pricing data persists across pod restarts
- Historical pricing data is available
- Multiple instances can share pricing data

### Local Cache Mode
When `PROMETHEUS_URL` is not set:
- SKU pricing data is stored in memory using TTLCache
- 24-hour cache duration
- Pricing data is lost on pod restart
- Each instance maintains its own pricing cache

## Running Locally

1. Set up GCP authentication:
```bash
export GOOGLE_APPLICATION_CREDENTIALS="path/to/your/service-account-key.json"
```

2. Configure environment variables:
```bash
export GCP_PROJECT_ID="your-project-id"
export KUBE_CONTEXT="your-context"  # Optional
export LOG_LEVEL="INFO"  # Optional
export PROMETHEUS_URL="http://prometheus:9090"  # Optional
```

3. Run the exporter:
```bash
python main.py
```

The exporter will start on port 8000 and expose the following metrics:
- `kube_pod_cpu_usage`: CPU usage per pod
- `kube_pod_memory_usage`: Memory usage per pod
- `gcp_pod_cpu_cost_hour`: Hourly CPU cost per pod
- `gcp_pod_memory_cost_hour`: Hourly memory cost per pod
- `gcp_pod_total_cost_hour`: Total hourly cost per pod
- `gcp_sku_price`: GCP SKU pricing information (when using Prometheus storage)

## Running in Kubernetes

1. Create a ConfigMap with your configuration:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: pod-cost-exporter-config
data:
  GCP_PROJECT_ID: "your-project-id"
  GCP_REGION: "us-central1"
  IN_CLUSTER: "true"
  PROMETHEUS_URL: "http://prometheus:9090"  # Optional
  PRICE_UPDATE_INTERVAL: "30"  # Optional
```

2. Deploy the exporter:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pod-cost-exporter
spec:
  selector:
    matchLabels:
      app: pod-cost-exporter
  template:
    metadata:
      labels:
        app: pod-cost-exporter
    spec:
      containers:
      - name: exporter
        image: pod-cost-exporter:latest
        envFrom:
        - configMapRef:
            name: pod-cost-exporter-config
        ports:
        - containerPort: 8000
```

## Prometheus Configuration

Add the following to your Prometheus scrape config:

```yaml
scrape_configs:
  - job_name: 'kubernetes-cost-metrics'
    static_configs:
      - targets: ['pod-cost-exporter:8000']
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
