$ErrorActionPreference = "Stop"

foreach ($tool in @("docker", "kind", "kubectl", "helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Output "$tool : not installed"
        continue
    }

    switch ($tool) {
        "docker" { docker version --format "Docker client={{.Client.Version}} server={{.Server.Version}}" }
        "kind" { kind version }
        "kubectl" { kubectl version --client }
        "helm" { helm version --short }
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not read the $tool version." }
}

if (Get-Command kubectl -ErrorAction SilentlyContinue) {
    kubectl get nodes -o wide
    if ($LASTEXITCODE -ne 0) { throw "Could not read Kubernetes node versions." }
}
