# Kubernetes Cost Exporter (k8scostexporter)

A Prometheus exporter that calculates and exposes cost metrics for Kubernetes resources (pods, storage) running on Google Cloud Platform (GCP).

## Features
- Real-time pod resource usage monitoring
- GCP pricing integration
- Cost calculation for pods and storage (storage coming soon)
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
```

3. Run the exporter:
```bash
python main.py
```

The exporter will start on port 8000 and expose the following metrics:
- `kube_pod_cpu_usage`
- `kube_pod_memory_usage`
- `gcp_pod_cpu_cost_hour`
- `gcp_pod_memory_cost_hour`
- `gcp_pod_total_cost_hour`

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
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
