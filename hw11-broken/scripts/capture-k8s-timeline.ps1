param(
    [int]$DurationSeconds = 180,
    [int]$IntervalSeconds = 2,
    [string]$Output = "results/kubernetes-timeline.jsonl"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputPath = Join-Path $projectRoot $Output
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $outputPath) | Out-Null
if (Test-Path $outputPath) { Remove-Item -LiteralPath $outputPath }
$deadline = (Get-Date).AddSeconds($DurationSeconds)

while ((Get-Date) -lt $deadline) {
    $pods = kubectl get pods -n hw11 -l "app.kubernetes.io/name=hw11-simulator,app.kubernetes.io/instance=hw11" -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not sample Pods." }
    $slices = kubectl get endpointslices -n hw11 -l kubernetes.io/service-name=hw11-hw11-simulator -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not sample EndpointSlices." }
    $hpa = kubectl get hpa hw11-hw11-simulator -n hw11 -o json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Could not sample the HPA." }

    $sample = [ordered]@{
        timestamp = (Get-Date).ToUniversalTime().ToString("o")
        pods = @($pods.items | ForEach-Object {
            [ordered]@{
                name = $_.metadata.name
                created = $_.metadata.creationTimestamp
                ready = [bool]($_.status.conditions | Where-Object { $_.type -eq "Ready" -and $_.status -eq "True" })
                phase = $_.status.phase
            }
        })
        endpoints = @($slices.items | ForEach-Object {
            $_.endpoints | ForEach-Object {
                [ordered]@{ pod = $_.targetRef.name; ready = $_.conditions.ready }
            }
        })
        hpa = [ordered]@{
            desiredReplicas = $hpa.status.desiredReplicas
            currentReplicas = $hpa.status.currentReplicas
            currentMetrics = $hpa.status.currentMetrics
            conditions = $hpa.status.conditions
        }
    }
    $sample | ConvertTo-Json -Depth 12 -Compress | Add-Content -LiteralPath $outputPath -Encoding utf8
    Start-Sleep -Seconds $IntervalSeconds
}
