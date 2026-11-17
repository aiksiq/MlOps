$ErrorActionPreference = "Stop"

$dockerBin = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"
if (Test-Path $dockerBin) {
    $env:PATH = "$dockerBin;$env:PATH"
}

$wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
foreach ($tool in @("kind", "helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        $packagePattern = if ($tool -eq "kind") { "Kubernetes.kind_*" } else { "Helm.Helm_*" }
        $package = Get-ChildItem $wingetPackages -Directory -Filter $packagePattern -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($package) {
            $binary = Get-ChildItem $package.FullName -Filter "$tool.exe" -File -Recurse -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($binary) {
                $env:PATH = "$(Split-Path -Parent $binary.FullName);$env:PATH"
            }
        }
    }
}

foreach ($tool in @("docker", "kind", "kubectl", "helm")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "Required command '$tool' is not installed or is not on PATH."
    }
}

docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine is unavailable. Start Docker Desktop and retry."
}

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    docker build -t hw11-simulator:0.1.0 .
    if ($LASTEXITCODE -ne 0) { throw "Docker image build failed." }

    $clusters = kind get clusters
    if ($LASTEXITCODE -ne 0) { throw "Could not list kind clusters." }
    if ($clusters -notcontains "hw11") {
        kind create cluster --name hw11 --config kind-config.yaml
        if ($LASTEXITCODE -ne 0) { throw "kind cluster creation failed." }
    }

    kind load docker-image hw11-simulator:0.1.0 --name hw11
    if ($LASTEXITCODE -ne 0) { throw "Loading the simulator image into kind failed." }

    docker pull registry.k8s.io/metrics-server/metrics-server:v0.7.2
    if ($LASTEXITCODE -ne 0) { throw "Pulling the metrics-server image failed." }
    $metricsArchive = Join-Path $env:TEMP "hw11-metrics-server-$PID.tar"
    docker image save --platform linux/amd64 --output $metricsArchive registry.k8s.io/metrics-server/metrics-server:v0.7.2
    if ($LASTEXITCODE -ne 0) { throw "Saving the metrics-server image failed." }
    try {
        kind load image-archive $metricsArchive --name hw11
        if ($LASTEXITCODE -ne 0) { throw "Loading the metrics-server image into kind failed." }
    }
    finally {
        Remove-Item -LiteralPath $metricsArchive -Force -ErrorAction SilentlyContinue
    }

    kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml
    if ($LASTEXITCODE -ne 0) { throw "Installing metrics-server failed." }

    $metricsArgs = kubectl get deployment metrics-server -n kube-system -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect metrics-server." }
    if ($metricsArgs.spec.template.spec.containers[0].args -notcontains "--kubelet-insecure-tls") {
        $containerArgs = @($metricsArgs.spec.template.spec.containers[0].args) + "--kubelet-insecure-tls"
        $patch = @{
            spec = @{
                template = @{
                    spec = @{
                        containers = @(@{
                            name = "metrics-server"
                            args = $containerArgs
                        })
                    }
                }
            }
        } | ConvertTo-Json -Depth 10 -Compress
        $patchFile = Join-Path $env:TEMP "hw11-metrics-server-patch-$PID.json"
        try {
            Set-Content -LiteralPath $patchFile -Value $patch -Encoding Ascii
            kubectl patch deployment metrics-server -n kube-system --type=strategic --patch-file $patchFile
            if ($LASTEXITCODE -ne 0) { throw "Configuring metrics-server for the local kind kubelets failed." }
        }
        finally {
            Remove-Item -LiteralPath $patchFile -Force -ErrorAction SilentlyContinue
        }
    }

    kubectl wait --for=condition=available deployment/metrics-server -n kube-system --timeout=120s
    if ($LASTEXITCODE -ne 0) { throw "metrics-server did not become available." }

    helm upgrade --install hw11 .\chart --namespace hw11 --create-namespace --reset-values --wait --timeout 5m
    if ($LASTEXITCODE -ne 0) { throw "Helm installation failed; inspect kubectl get pods -n hw11." }

    kubectl get pods,services,hpa -n hw11 -o wide
    $metricsReady = $false
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        kubectl top pods -n hw11
        if ($LASTEXITCODE -eq 0) {
            $metricsReady = $true
            break
        }
        Start-Sleep -Seconds 5
    }
    if (-not $metricsReady) { throw "Resource metrics did not become available; inspect metrics-server logs." }
}
finally {
    Pop-Location
}
